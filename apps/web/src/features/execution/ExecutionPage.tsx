import { CheckCircleOutlined, SaveOutlined, SendOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Form, Input, InputNumber, Select, Space, Spin, Tag, Typography, message } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { approveDevicePlan, executeHuaweiPlan, executionEventStreamUrl, listConfigTasks, listPlanExecutions, type ConfigTask, type ExecutionCommand, type ExecutionRun } from "../../api/client";

type ConnectionForm = { host: string; port: number; username: string; password: string };

const terminalExecutionStatuses = new Set(["preflight_blocked", "validation_failed", "command_failed", "completed", "failed"]);

function mergeExecutionEntries(current: ExecutionCommand[], incoming: ExecutionCommand[]): ExecutionCommand[] {
  const bySequence = new Map(current.map((item) => [item.sequence, item]));
  incoming.forEach((item) => bySequence.set(item.sequence, item));
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
}

function executionStatusLabel(status?: string): string {
  return ({
    queued: "排队中",
    running: "执行中",
    completed: "已完成",
    failed: "失败",
    command_failed: "命令失败",
    validation_failed: "验证失败",
    preflight_blocked: "预检阻止",
  } as Record<string, string>)[status || ""] || "等待提交";
}

export function ExecutionPage() {
  const queryClient = useQueryClient();
  const [form] = Form.useForm<ConnectionForm>();
  const [selectedTaskId, setSelectedTaskId] = useState<string>();
  const [selectedPlanId, setSelectedPlanId] = useState<string>();
  const [result, setResult] = useState<ExecutionRun>();
  const [activeExecutionId, setActiveExecutionId] = useState<string>();
  const [liveEntries, setLiveEntries] = useState<ExecutionCommand[]>([]);
  const [commandDraft, setCommandDraft] = useState("");
  const streamRef = useRef<EventSource | null>(null);
  const tasks = useQuery({ queryKey: ["config-tasks"], queryFn: listConfigTasks, refetchInterval: 10_000 });
  const refetchTasks = tasks.refetch;
  const task = useMemo(() => tasks.data?.find((item) => item.id === selectedTaskId), [selectedTaskId, tasks.data]);
  const plan = useMemo(() => task?.device_plans.find((item) => item.id === selectedPlanId), [selectedPlanId, task]);
  const savedCommands = plan?.commands.join("\n") || "";
  const executions = useQuery({
    queryKey: ["plan-executions", task?.id, plan?.id],
    queryFn: () => listPlanExecutions(task!.id, plan!.id),
    enabled: Boolean(task && plan),
    refetchInterval: activeExecutionId ? 1_000 : false,
  });

  const closeExecutionStream = () => {
    streamRef.current?.close();
    streamRef.current = null;
    setActiveExecutionId(undefined);
  };
  const openExecutionStream = (executionId: string) => {
    streamRef.current?.close();
    const stream = new EventSource(executionEventStreamUrl(executionId));
    stream.addEventListener("execution", (event) => {
      const entry = JSON.parse((event as MessageEvent<string>).data) as ExecutionCommand;
      setLiveEntries((current) => mergeExecutionEntries(current, [entry]));
    });
    stream.addEventListener("complete", (event) => {
      const completed = JSON.parse((event as MessageEvent<string>).data) as ExecutionRun;
      setResult(completed);
      executions.refetch();
      tasks.refetch();
      if (streamRef.current === stream) closeExecutionStream();
    });
    stream.onerror = () => {
      // Keep polling active when SSE is interrupted. The execution history is
      // durable in SQLite, so polling can restore every missed record.
      if (stream.readyState === EventSource.CLOSED && streamRef.current === stream) {
        streamRef.current = null;
      }
    };
    streamRef.current = stream;
  };
  useEffect(() => () => streamRef.current?.close(), []);

  useEffect(() => {
    if (!selectedTaskId && tasks.data?.[0]) setSelectedTaskId(tasks.data[0].id);
  }, [selectedTaskId, tasks.data]);
  useEffect(() => {
    const firstPlan = task?.device_plans[0];
    if (firstPlan && !task?.device_plans.some((item) => item.id === selectedPlanId)) setSelectedPlanId(firstPlan.id);
  }, [selectedPlanId, task]);
  useEffect(() => {
    setResult(undefined);
    setLiveEntries([]);
    streamRef.current?.close();
    streamRef.current = null;
    setActiveExecutionId(undefined);
  }, [selectedPlanId]);
  useEffect(() => {
    if (!plan) return;
    form.setFieldsValue({ host: plan.connection_hint.host || "", port: plan.connection_hint.port || 22, username: plan.connection_hint.username || "", password: "" });
  }, [form, plan]);
  useEffect(() => {
    setCommandDraft(savedCommands);
  }, [plan?.approval_revision, plan?.id, savedCommands]);
  useEffect(() => {
    if (!activeExecutionId) return;
    const current = (executions.data || []).find((item) => item.id === activeExecutionId);
    if (!current) return;
    setLiveEntries((entries) => mergeExecutionEntries(entries, current.commands));
    setResult(current);
    if (terminalExecutionStatuses.has(current.status)) {
      streamRef.current?.close();
      streamRef.current = null;
      setActiveExecutionId(undefined);
      void refetchTasks();
    }
  }, [activeExecutionId, executions.data, refetchTasks]);

  const prepareExecutionTracking = (executionId: string) => {
    streamRef.current?.close();
    streamRef.current = null;
    setLiveEntries([]);
    setResult(undefined);
    setActiveExecutionId(executionId);
  };

  const apply = useMutation({
    mutationFn: ({ values, executionId }: { values: ConnectionForm; executionId: string }) => {
      if (!task || !plan) return Promise.reject(new Error("请选择设备计划"));
      return executeHuaweiPlan(task.id, plan.id, { ...values, execution_id: executionId });
    },
    onSuccess: (run) => {
      setResult(run);
      openExecutionStream(run.id);
      void executions.refetch();
      message.info("下发任务已提交，请在右侧查看设备实时回显。");
    },
    onError: () => { closeExecutionStream(); message.error("下发请求失败；设备未自动重试。"); },
  });
  const saveCommands = useMutation({
    mutationFn: () => {
      if (!task || !plan) return Promise.reject(new Error("请选择设备计划"));
      const commands = commandDraft.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      if (!commands.length) return Promise.reject(new Error("命令不能为空"));
      return approveDevicePlan(task.id, plan.id, {
        approval_revision: plan.approval_revision,
        command_overrides: commands,
      });
    },
    onSuccess: (updated) => {
      setCommandDraft(updated.commands.join("\n"));
      queryClient.setQueryData<ConfigTask[]>(["config-tasks"], (current) => current?.map((item) => (
        item.id === task?.id
          ? { ...item, device_plans: item.device_plans.map((candidate) => candidate.id === updated.id ? updated : candidate) }
          : item
      )));
      void tasks.refetch();
      message.success("本设备命令已保存；下发将使用此版本。");
    },
    onError: () => message.error("命令保存失败；请刷新设备计划后重试。"),
  });
  const taskOptions = (tasks.data || []).map((item) => ({ value: item.id, label: `${item.id.slice(0, 8)} · ${item.status} · ${item.requirement_text.slice(0, 28)}` }));
  const planOptions = (task?.device_plans || []).map((item) => ({ value: item.id, label: `${item.display_name} · ${item.approved_at ? "已审批" : "待审批"} · ${item.compatibility_status}` }));
  const normalizedDraft = commandDraft.split(/\r?\n/).map((item) => item.trim()).filter(Boolean).join("\n");
  const draftDirty = normalizedDraft !== savedCommands;
  const executable = Boolean(plan?.approved_at && plan.commands.length && !draftDirty);
  const running = Boolean(activeExecutionId);
  const submitApply = (values: ConnectionForm) => {
    const executionId = crypto.randomUUID().replace(/-/g, "");
    // Queue first, then subscribe from sequence zero. The SSE endpoint and
    // polling both replay durable rows, so no early device event can be lost.
    prepareExecutionTracking(executionId);
    apply.mutate({ values, executionId });
  };
  const activeRun = activeExecutionId ? (executions.data || []).find((item) => item.id === activeExecutionId) : undefined;
  const executionStatus = activeRun?.status || (activeExecutionId ? "queued" : result?.status);

  return <>
    <Typography.Title level={2} className="page-title">下发与结果</Typography.Title>
    <Typography.Text type="secondary" className="page-subtitle">选择已生成的设备计划，可在此修改并保存命令；确认后只下发当前设备。右侧会实时显示命令与设备回显，密码只在本次 SSH 连接中使用。</Typography.Text>
    <Alert type="warning" showIcon style={{ margin: "16px 0" }} message="逐台确认下发" description="每次只发送当前设备已确认的命令。系统会记录设备返回和验证结果；显式标记的受保护端口不会写入。" />
    <div className="execution-layout">
      <main className="execution-main">
        <Card title="选择设备并确认下发" className="execution-card">
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Select loading={tasks.isLoading} value={selectedTaskId} options={taskOptions} placeholder="选择最近生成的配置任务" onChange={(value) => { setSelectedTaskId(value); setSelectedPlanId(undefined); }} />
            <Select disabled={!task} value={selectedPlanId} options={planOptions} placeholder="选择本次下发的设备" onChange={setSelectedPlanId} />
            {!plan && <Empty description="选择一个设备计划以查看命令与连接信息" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            {plan && <>
              <Descriptions size="small" column={{ xs: 1, md: 3 }} className="plan-summary">
                <Descriptions.Item label="设备">{plan.display_name}</Descriptions.Item>
                <Descriptions.Item label="现场型号（审计）">{plan.detected_model || "未查询"}</Descriptions.Item>
                <Descriptions.Item label="审批"><Tag color={plan.approved_at ? "success" : "warning"}>{plan.approved_at ? "已审批" : "待审批"}</Tag></Descriptions.Item>
              </Descriptions>
              {!executable && <Alert type="info" showIcon message="此计划尚不能下发" description={draftDirty ? "命令已修改但尚未保存；请先保存当前命令版本。" : "请先回到“配置规划”确认一组非空命令。"} />}
              <div>
                <div className="section-label">本设备待执行命令（可编辑）</div>
                <Input.TextArea className="command-row" autoSize={{ minRows: 8, maxRows: 32 }} value={commandDraft} onChange={(event) => setCommandDraft(event.target.value)} placeholder="可在此修订命令；保存后下发页将使用此版本。" disabled={running} />
                <Space style={{ marginTop: 10 }}>
                  <Button icon={<SaveOutlined />} loading={saveCommands.isPending} disabled={!normalizedDraft || running} onClick={() => saveCommands.mutate()}>保存命令</Button>
                  {draftDirty && <Tag color="gold">有未保存修改</Tag>}
                </Space>
              </div>
              <Form form={form} layout="vertical" onFinish={submitApply}>
                <div className="connection-grid">
                  <Form.Item name="host" label="冻结 SSH 地址" rules={[{ required: true }]}><Input /></Form.Item>
                  <Form.Item name="port" label="端口" rules={[{ required: true }]}><InputNumber min={1} max={65535} style={{ width: "100%" }} /></Form.Item>
                  <Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input /></Form.Item>
                  <Form.Item name="password" label="本次 SSH 密码" rules={[{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item>
                </div>
                <Space wrap>
                  <Button type="primary" danger htmlType="submit" icon={<SendOutlined />} loading={apply.isPending} disabled={!executable || running}>确认仅下发 {plan.display_name}</Button>
                </Space>
              </Form>
            </>}
          </Space>
        </Card>
        {result && <Card title={<Space><CheckCircleOutlined />执行记录 {result.id.slice(0, 8)} · 下发</Space>} style={{ marginTop: 16 }}>
          <Descriptions size="small" column={{ xs: 1, md: 2 }}><Descriptions.Item label="状态"><Tag color={result.status === "completed" ? "success" : result.status === "running" || result.status === "queued" ? "processing" : "error"}>{result.status}</Tag></Descriptions.Item><Descriptions.Item label="错误">{result.error_message || "-"}</Descriptions.Item><Descriptions.Item label="预检">{(result.preflight.errors || []).join("；") || "通过"}</Descriptions.Item><Descriptions.Item label="保存">{JSON.stringify(result.save)}</Descriptions.Item></Descriptions>
          <pre className="execution-live-output execution-result-terminal">{terminalOutput(result.commands) || "暂无设备回显"}</pre>
        </Card>}
      </main>
      <aside className="execution-stream-sidebar"><ExecutionProgressPanel entries={liveEntries} running={running} status={executionStatus} /></aside>
    </div>
  </>;
}

function ExecutionProgressPanel({ entries, running, status }: { entries: ExecutionCommand[]; running: boolean; status?: string }) {
  const rawOutput = terminalOutput(entries);
  const tagColor = status === "completed" ? "success" : terminalExecutionStatuses.has(status || "") ? "error" : running ? "processing" : "default";
  return <Card size="small" title="设备实时回显" extra={<Space size={6}>{running && <Spin size="small" />}<Tag color={tagColor}>{executionStatusLabel(status)}</Tag></Space>}>
    {rawOutput ? <pre className="execution-live-output execution-live-terminal">{rawOutput}</pre> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={running ? `${executionStatusLabel(status)}，正在等待设备返回回显` : "提交下发后将在这里显示设备回显"} />}
  </Card>;
}

function terminalOutput(entries: ExecutionCommand[]): string {
  return entries
    .slice()
    .sort((left, right) => left.sequence - right.sequence)
    .map((entry) => entry.output)
    .filter(Boolean)
    .join("\n");
}
