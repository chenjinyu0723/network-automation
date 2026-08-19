import { CheckCircleOutlined, PlayCircleOutlined, SafetyCertificateOutlined, SendOutlined, UndoOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Form, Input, InputNumber, List, Modal, Select, Space, Spin, Tag, Typography, message } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { executeHuaweiPlan, executePcPing, executionEventStreamUrl, listConfigTasks, listPlanExecutions, type ConfigTask, type DevicePlan, type ExecutionCommand, type ExecutionRun, type PcPingRun, undoHuaweiPlan } from "../../api/client";

type ConnectionForm = { host: string; port: number; username: string; password: string };
type PingForm = { host: string; port: number; username: string; password: string; os_family: "linux" | "windows"; target_ip: string };

export function ExecutionPage() {
  const [form] = Form.useForm<ConnectionForm>();
  const [pingForm] = Form.useForm<PingForm>();
  const [selectedTaskId, setSelectedTaskId] = useState<string>();
  const [selectedPlanId, setSelectedPlanId] = useState<string>();
  const [result, setResult] = useState<ExecutionRun>();
  const [ping, setPing] = useState<PcPingRun>();
  const [activeExecutionId, setActiveExecutionId] = useState<string>();
  const [liveEntries, setLiveEntries] = useState<ExecutionCommand[]>([]);
  const streamRef = useRef<EventSource | null>(null);
  const tasks = useQuery({ queryKey: ["config-tasks"], queryFn: listConfigTasks, refetchInterval: 10_000 });
  const task = useMemo(() => tasks.data?.find((item) => item.id === selectedTaskId), [selectedTaskId, tasks.data]);
  const plan = useMemo(() => task?.device_plans.find((item) => item.id === selectedPlanId), [selectedPlanId, task]);
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
    setLiveEntries([]);
    setActiveExecutionId(executionId);
    const stream = new EventSource(executionEventStreamUrl(executionId));
    stream.addEventListener("execution", (event) => {
      const entry = JSON.parse((event as MessageEvent<string>).data) as ExecutionCommand;
      setLiveEntries((current) => current.some((item) => item.sequence === entry.sequence) ? current : [...current, entry]);
    });
    stream.addEventListener("complete", (event) => {
      const completed = JSON.parse((event as MessageEvent<string>).data) as ExecutionRun;
      setResult(completed);
      executions.refetch();
      tasks.refetch();
      if (streamRef.current === stream) closeExecutionStream();
    });
    stream.onerror = () => {
      if (stream.readyState === EventSource.CLOSED && streamRef.current === stream) closeExecutionStream();
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
    setPing(undefined);
    setLiveEntries([]);
  }, [selectedPlanId]);
  useEffect(() => {
    if (!plan) return;
    form.setFieldsValue({ host: plan.connection_hint.host || "", port: plan.connection_hint.port || 22, username: plan.connection_hint.username || "", password: "" });
  }, [form, plan]);

  const apply = useMutation({
    mutationFn: ({ values, executionId }: { values: ConnectionForm; executionId: string }) => {
      if (!task || !plan) return Promise.reject(new Error("请选择设备计划"));
      return executeHuaweiPlan(task.id, plan.id, { ...values, execution_id: executionId });
    },
    onSuccess: (run) => { setResult(run); message.info("下发任务已提交，请在右侧查看设备实时回显。"); },
    onError: () => { closeExecutionStream(); message.error("下发请求失败；设备未自动重试。"); },
  });
  const undo = useMutation({
    mutationFn: ({ values, executionId }: { values: ConnectionForm; executionId: string }) => {
      if (!task || !plan) return Promise.reject(new Error("请选择设备计划"));
      return undoHuaweiPlan(task.id, plan.id, { ...values, execution_id: executionId });
    },
    onSuccess: (run) => { setResult(run); message.info("Undo 已提交，请在右侧查看设备实时回显。"); },
    onError: () => { closeExecutionStream(); message.error("Undo 请求失败；请检查该设备是否存在成功下发记录。"); },
  });
  const pingMutation = useMutation({
    mutationFn: (values: PingForm) => result ? executePcPing(result.id, values) : Promise.reject(new Error("设备执行不存在")),
    onSuccess: (run) => { setPing(run); message.info(run.success ? "PC ping 验收通过。" : "PC ping 未通过，请检查输出。"); },
    onError: () => message.error("PC ping 验收无法执行；设备必须先完成验证与 save。"),
  });

  const taskOptions = (tasks.data || []).map((item) => ({ value: item.id, label: `${item.id.slice(0, 8)} · ${item.status} · ${item.requirement_text.slice(0, 28)}` }));
  const planOptions = (task?.device_plans || []).map((item) => ({ value: item.id, label: `${item.display_name} · ${item.approved_at ? "已审批" : "待审批"} · ${item.compatibility_status}` }));
  const executable = Boolean(plan?.approved_at && plan.commands.length);
  const history = useMemo(() => {
    const items = [...(executions.data || [])];
    if (result && !items.some((item) => item.id === result.id)) items.push(result);
    return items.sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime());
  }, [executions.data, result]);
  const latestApply = [...history].reverse().find((item) => item.operation === "apply" && item.status === "completed");
  const latestUndo = [...history].reverse().find((item) => item.operation === "undo" && item.status === "completed");
  const undoAvailable = Boolean(
    plan?.rollback?.level === "conditional"
    && plan.rollback.commands?.length
    && latestApply
    && (!latestUndo || new Date(latestUndo.created_at) < new Date(latestApply.created_at)),
  );
  const running = Boolean(activeExecutionId);
  const submitApply = (values: ConnectionForm) => {
    const executionId = crypto.randomUUID().replace(/-/g, "");
    openExecutionStream(executionId);
    apply.mutate({ values, executionId });
  };
  const submitUndo = async () => {
    try {
      const values = await form.validateFields();
      Modal.confirm({
        title: `确认 Undo ${plan?.display_name || "当前设备"}`,
        content: plan?.rollback?.reason || "将只执行当前设备计划中的受限回滚草案，并保存设备配置。",
        okText: "确认执行 Undo",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: () => {
          const executionId = crypto.randomUUID().replace(/-/g, "");
          openExecutionStream(executionId);
          undo.mutate({ values, executionId });
        },
      });
    } catch {
      message.error("请先填写当前设备的 SSH 地址、端口、用户名和密码。");
    }
  };

  return <>
    <Typography.Title level={2} className="page-title">下发与结果</Typography.Title>
    <Typography.Text type="secondary" className="page-subtitle">选择已生成的设备计划，确认后只下发当前设备。右侧会实时显示命令与设备回显，密码只在本次 SSH 连接中使用。</Typography.Text>
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
              {!executable && <Alert type="info" showIcon message="此计划尚不能下发" description="请先回到“配置规划”确认一组非空命令。" />}
              <div className="command-preview"><div className="section-label">本设备待执行命令</div>{plan.commands.length ? plan.commands.join("\n") : "无可执行命令"}</div>
              <Form form={form} layout="vertical" onFinish={submitApply}>
                <div className="connection-grid">
                  <Form.Item name="host" label="冻结 SSH 地址" rules={[{ required: true }]}><Input /></Form.Item>
                  <Form.Item name="port" label="端口" rules={[{ required: true }]}><InputNumber min={1} max={65535} style={{ width: "100%" }} /></Form.Item>
                  <Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input /></Form.Item>
                  <Form.Item name="password" label="本次 SSH 密码" rules={[{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item>
                </div>
                <Space wrap>
                  <Button type="primary" danger htmlType="submit" icon={<SendOutlined />} loading={apply.isPending} disabled={!executable || running}>确认仅下发 {plan.display_name}</Button>
                  <Button icon={<UndoOutlined />} title="执行当前设备最近一次成功下发的受限回滚草案" loading={undo.isPending} disabled={!undoAvailable || running} onClick={submitUndo}>Undo 上次下发</Button>
                </Space>
              </Form>
              {plan.rollback?.reason && <Typography.Paragraph type="secondary" style={{ margin: 0 }}>Undo 条件：{plan.rollback.reason}</Typography.Paragraph>}
            </>}
          </Space>
        </Card>
        {result && <Card title={<Space><CheckCircleOutlined />执行记录 {result.id.slice(0, 8)} · {result.operation === "undo" ? "Undo" : "下发"}</Space>} style={{ marginTop: 16 }}>
          <Descriptions size="small" column={{ xs: 1, md: 2 }}><Descriptions.Item label="状态"><Tag color={result.status === "completed" ? "success" : result.status === "running" || result.status === "queued" ? "processing" : "error"}>{result.status}</Tag></Descriptions.Item><Descriptions.Item label="错误">{result.error_message || "-"}</Descriptions.Item><Descriptions.Item label="预检">{(result.preflight.errors || []).join("；") || "通过"}</Descriptions.Item><Descriptions.Item label="保存">{JSON.stringify(result.save)}</Descriptions.Item></Descriptions>
          <List size="small" dataSource={result.commands} renderItem={(entry) => <List.Item><Space direction="vertical" size={0}><Typography.Text code>{entry.phase} · {entry.command}</Typography.Text><Typography.Text type={entry.success ? "secondary" : "danger"}>{entry.output || (entry.success ? "已发送" : "失败")}</Typography.Text></Space></List.Item>} />
        </Card>}
        {result?.status === "completed" && result.operation === "apply" && <Card title={<Space><PlayCircleOutlined />PC SSH ping 验收（可选）</Space>} style={{ marginTop: 16 }}>
          <Alert type="info" showIcon icon={<SafetyCertificateOutlined />} message="仅允许 ping 到明确填写的 IPv4 地址；PC 密码不保存。" style={{ marginBottom: 12 }} />
          <Form form={pingForm} layout="vertical" initialValues={{ port: 22, os_family: "linux" }} onFinish={(values) => pingMutation.mutate(values)}><div className="connection-grid"><Form.Item name="host" label="PC SSH IP" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="port" label="端口"><InputNumber min={1} max={65535} style={{ width: "100%" }} /></Form.Item><Form.Item name="username" label="用户" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="password" label="密码" rules={[{ required: true }]}><Input.Password /></Form.Item><Form.Item name="target_ip" label="Ping 目标 IPv4" rules={[{ required: true }]}><Input /></Form.Item></div><Button htmlType="submit" loading={pingMutation.isPending}>执行 ping</Button></Form>
          {ping && <Typography.Paragraph style={{ marginTop: 12 }}><Typography.Text type={ping.success ? "success" : "danger"}>{ping.success ? "通过" : ping.error_message || "未通过"}</Typography.Text><pre>{ping.output || ping.command}</pre></Typography.Paragraph>}
        </Card>}
      </main>
      <aside className="execution-stream-sidebar"><ExecutionProgressPanel entries={liveEntries} running={running} /></aside>
    </div>
  </>;
}

function ExecutionProgressPanel({ entries, running }: { entries: ExecutionCommand[]; running: boolean }) {
  return <Card size="small" title="设备实时回显" extra={running ? <Space size={6}><Spin size="small" /><Typography.Text type="secondary">执行中</Typography.Text></Space> : <Tag>空闲</Tag>}>
    {entries.length ? <List className="execution-live-list" size="small" dataSource={entries} renderItem={(entry) => <List.Item><div className="execution-live-entry"><Typography.Text code>{entry.phase} · {entry.command}</Typography.Text><div className={entry.success ? "execution-live-output" : "execution-live-output execution-live-error"}>{entry.output || "等待设备返回..."}</div></div></List.Item>} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={running ? "正在等待设备建立 SSH 连接" : "提交下发或 Undo 后将在这里显示设备回显"} />}
  </Card>;
}
