import { CheckOutlined, PlayCircleOutlined, SaveOutlined, SearchOutlined, StopOutlined } from "@ant-design/icons";
import { isAxiosError } from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Collapse, Descriptions, Empty, Input, List, Modal, Select, Space, Spin, Tag, Timeline, Typography, message } from "antd";
import { useEffect, useRef, useState } from "react";
import { approveDevicePlan, cancelConfigTask, createConfigTask, generateConfigCommands, getConfigTask, listManuals, listTemplates, listTopologies, planningEventStreamUrl, saveTaskAsTemplate, searchCommands, updatePlanningIdea, type CommandHit, type ConfigTask, type PlanningEvent } from "../../api/client";

export function PlanningPage() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [revisionId, setRevisionId] = useState("");
  const [manualId, setManualId] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [requirement, setRequirement] = useState("");
  const [task, setTask] = useState<ConfigTask | null>(null);
  const [planningIdea, setPlanningIdea] = useState("");
  const [saveTemplateOpen, setSaveTemplateOpen] = useState(false);
  const [templateTitle, setTemplateTitle] = useState("");
  const [templateDescription, setTemplateDescription] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [planningEvents, setPlanningEvents] = useState<PlanningEvent[]>([]);
  const [currentDeviceName, setCurrentDeviceName] = useState<string | null>(null);
  const [selectedPlanId, setSelectedPlanId] = useState<string | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const manuals = useQuery({ queryKey: ["manuals"], queryFn: listManuals });
  const topologies = useQuery({ queryKey: ["topologies"], queryFn: listTopologies });
  const templates = useQuery({ queryKey: ["templates"], queryFn: listTemplates });
  const search = useMutation({ mutationFn: (value: string) => searchCommands(value) });
  const closeStream = (expected?: EventSource) => {
    if (expected && streamRef.current !== expected) return;
    streamRef.current?.close();
    streamRef.current = null;
    setActiveRunId(null);
  };
  const refreshCompletedTask = async (taskId: string) => {
    const result = await getConfigTask(taskId);
    setTask(result);
    setPlanningIdea(result.planning_idea);
    return result;
  };
  const openStream = (taskId: string, after = 0) => {
    streamRef.current?.close();
    setPlanningEvents([]);
    setActiveRunId(taskId);
    const stream = new EventSource(planningEventStreamUrl(taskId, after));
    stream.addEventListener("planning", (event) => {
      const next = JSON.parse((event as MessageEvent<string>).data) as PlanningEvent;
      setPlanningEvents((current) => current.some((item) => item.sequence === next.sequence) ? current : [...current, next]);
      if (next.stage.includes(" · ")) {
        const [deviceName, stage] = next.stage.split(" · ", 2);
        if (stage === "设备规划") setCurrentDeviceName(deviceName);
        if (next.event_type === "output" && stage === "设备规划") {
          void refreshCompletedTask(taskId).then((result) => {
            const completed = result.device_plans.find((plan) => plan.display_name === deviceName);
            if (completed) setSelectedPlanId(completed.id);
          }).catch(() => undefined);
        }
      }
    });
    stream.addEventListener("complete", () => {
      if (streamRef.current !== stream) return;
      void refreshCompletedTask(taskId)
        .catch(() => message.error("任务已结束，但读取最终结果失败；请刷新页面后重试。"))
        .finally(() => closeStream(stream));
    });
    stream.onerror = () => {
      if (stream.readyState === EventSource.CLOSED) closeStream(stream);
    };
    streamRef.current = stream;
  };
  useEffect(() => () => streamRef.current?.close(), []);
  const createTask = useMutation({
    mutationFn: createConfigTask,
    onSuccess: (result) => { setTask(result); setSelectedPlanId(null); setCurrentDeviceName(null); setPlanningIdea(result.planning_idea); message.success("配置思路已生成，请先审阅或修改思路。"); },
    onError: () => { closeStream(); message.error("创建任务失败；请先从拓扑页保存 revision，并选择已完成抽取的手册。"); }
  });
  const saveIdea = useMutation({
    mutationFn: () => task ? updatePlanningIdea(task.id, planningIdea) : Promise.reject(new Error("任务不存在")),
    onSuccess: (result) => { setTask(result); setPlanningIdea(result.planning_idea); message.success("配置思路已保存，尚未生成命令。"); },
    onError: () => message.error("配置思路保存失败。")
  });
  const generateCommands = useMutation({
    mutationFn: () => task ? generateConfigCommands(task.id, planningIdea) : Promise.reject(new Error("任务不存在")),
    onSuccess: (result) => { setTask(result); setPlanningIdea(result.planning_idea); message.success("命令生成任务已开始，进度和结果将显示在右侧。"); },
    onError: (error: unknown) => {
      const detail = isAxiosError<{ detail?: string }>(error) ? error.response?.data?.detail : undefined;
      if (isAxiosError(error) && error.response?.status === 409) {
        // The first click may already have queued a long-running worker.  Keep
        // the freshly opened stream alive so a repeat click never hides progress.
        message.info(detail || "任务仍在运行，已继续订阅右侧进度。");
        return;
      }
      closeStream();
      message.error(detail || (error instanceof Error ? error.message : "命令生成失败。"));
    }
  });
  const approve = useMutation({
    mutationFn: ({ planId, revision, commands }: { planId: string; revision: number; commands: string[] }) => {
      if (!task) return Promise.reject(new Error("任务不存在"));
      return approveDevicePlan(task.id, planId, { approval_revision: revision, command_overrides: commands });
    },
       onSuccess: (updated) => {
      setTask((current) => current ? { ...current, device_plans: current.device_plans.map((item) => item.id === updated.id ? updated : item) } : current);
       message.success("本设备命令已保存，可在下发页逐台使用。");
    },
    onError: () => message.error("确认失败；请确保命令列表不为空并重新审阅。")
  });
  const saveTemplate = useMutation({
    mutationFn: () => task ? saveTaskAsTemplate(task.id, { title: templateTitle, description: templateDescription }) : Promise.reject(new Error("任务不存在")),
    onSuccess: () => {
      setSaveTemplateOpen(false);
      setTemplateTitle("");
      setTemplateDescription("");
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      message.success("配置模板已保存为不可变快照。");
    },
    onError: () => message.error("模板保存失败；请先生成设备命令并填写标题。")
  });
  const stopPlanning = useMutation({
    mutationFn: () => activeRunId ? cancelConfigTask(activeRunId) : Promise.reject(new Error("当前没有运行中的规划任务")),
    onSuccess: (result) => {
      setTask(result);
      message.info("已请求停止任务；当前模型响应流会在可取消位置结束。");
    },
    onError: () => message.error("停止任务失败；任务可能已经结束。")
  });
  const startIdea = () => {
    const taskId = crypto.randomUUID().replace(/-/g, "");
    openStream(taskId);
    createTask.mutate({ task_id: taskId, topology_revision_id: revisionId, manual_id: manualId, requirement_text: requirement, template_id: templateId || undefined });
  };
  const startCommands = () => {
    if (!task) return;
    // A task keeps the first-stage log for audit.  Subscribe after its last event
    // so this command-generation run shows only its own live output.
    const lastSequence = planningEvents.reduce((latest, event) => Math.max(latest, event.sequence), 0);
    openStream(task.id, lastSequence);
    generateCommands.mutate();
  };
  const submit = () => { if (query.trim()) search.mutate(query.trim()); };
  return (
    <>
      <Typography.Title level={2} className="page-title">配置规划</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">先生成配置思路，再由你确认或修改；思路为空时不会进入命令生成阶段。可选模板只作为 LLM 的角色划分、实施顺序与命令组织参考。</Typography.Text>
      <div className="planning-layout">
      <main className="planning-main">
      <Alert type="info" showIcon message="两阶段规划" description="第一阶段只生成可编辑的配置思路。你确认思路后，系统才会检索手册并生成逐设备命令草案。模板中的设备、端口、VLAN 和地址不会直接复制到当前任务。" style={{ marginBottom: 16 }} />
      <Input.Search value={query} onChange={(event) => setQuery(event.target.value)} enterButton={<SearchOutlined />} loading={search.isPending} onSearch={submit} placeholder="查询已选手册中的命令，例如：vlan batch、port link-type、display version" style={{ width: "100%", maxWidth: 700, marginBottom: 16 }} />
      <Card title="手册命令证据">
        <List
          loading={search.isPending}
          dataSource={search.data || []}
          className="manual-evidence-list"
          pagination={{ pageSize: 10, size: "small", showSizeChanger: false, hideOnSinglePage: true }}
          locale={{ emptyText: "输入关键词后查询已注入手册" }}
          renderItem={(item: CommandHit) => <List.Item><List.Item.Meta title={<Space><Typography.Text code>{item.canonical_name}</Typography.Text><Tag>{item.feature || "未分类"}</Tag><Tag color="blue">{item.applicability_mode}</Tag>{item.retrieval_sources.map((source) => <Tag key={source} color="purple">{source}</Tag>)}</Space>} description={<div><div className="command-row">{item.syntax.join("\n")}</div><Typography.Text type="secondary">来源：{item.source_path}；混合分数：{item.score?.toFixed(3) ?? "-"}</Typography.Text></div>} /></List.Item>}
        />
      </Card>
      <Card title="创建配置任务" style={{ marginTop: 16 }} extra={<Tag color="gold">命令仅为草案，是否下发由用户决定</Tag>}>
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Select
            value={revisionId || undefined}
            onChange={setRevisionId}
            placeholder="选择已保存拓扑"
            showSearch
            optionFilterProp="label"
            options={(topologies.data || []).map((item) => ({ value: item.revision_id, label: item.name }))}
          />
          <Select value={manualId || undefined} onChange={setManualId} placeholder="选择已完成抽取、用于本次命令检索的手册" options={(manuals.data || []).filter((item) => item.status.startsWith("completed")).map((item) => ({ value: item.id, label: `${item.brand || "未知"} · ${item.original_filename} · ${item.release || "未标注"}` }))} />
          <Select allowClear value={templateId || undefined} onChange={(value) => setTemplateId(value || "")} placeholder="可选：选择模板作为 LLM 规划参考，不会复制旧设备或命令" options={(templates.data || []).map((item) => ({ value: item.id, label: `${item.title}${item.description ? ` · ${item.description}` : ""}` }))} />
          <Input.TextArea value={requirement} onChange={(event) => setRequirement(event.target.value)} rows={3} placeholder="填写本次网络配置要求，例如终端的 VLAN、互通范围、网关位置和链路承载要求" />
          <Button type="primary" icon={<PlayCircleOutlined />} loading={createTask.isPending} disabled={!revisionId || !manualId || requirement.trim().length < 3 || Boolean(activeRunId)} onClick={startIdea}>第一步：生成配置思路</Button>
          {task && <>
            <Card size="small" title="第一步：配置思路（可编辑）" style={{ marginTop: 8 }}>
              <Input.TextArea value={planningIdea} onChange={(event) => setPlanningIdea(event.target.value)} autoSize={{ minRows: 12, maxRows: 32 }} placeholder="填写或修订配置思路：设备角色、VLAN、端口、网关、实施顺序和约束" />
              <Space style={{ marginTop: 12 }} wrap>
                <Button loading={saveIdea.isPending} onClick={() => saveIdea.mutate()} disabled={!task || saveIdea.isPending}>保存思路</Button>
                <Button type="primary" loading={generateCommands.isPending} onClick={startCommands} disabled={!planningIdea.trim() || generateCommands.isPending || Boolean(activeRunId)}>确认思路并生成命令</Button>
                <Tag color={task.planning_idea_confirmed_at ? "success" : "gold"}>{task.planning_idea_confirmed_at ? "思路已确认" : "等待确认"}</Tag>
              </Space>
              <Typography.Paragraph type="secondary" style={{ marginTop: 10, marginBottom: 0 }}>调整 VLAN、设备、端口或 IP 等结构化事实时，请同步修改需求或拓扑；这里的思路会作为已确认规划上下文提供给后续命令规划。</Typography.Paragraph>
            </Card>
            {task.device_plans.length > 0 && <TaskReview task={task} selectedPlanId={selectedPlanId} onSelectPlan={setSelectedPlanId} savePlan={(planId, revision, commands) => approve.mutate({ planId, revision, commands })} saving={approve.isPending} onSaveTemplate={() => setSaveTemplateOpen(true)} />}
          </>}
        </Space>
      </Card>
      </main>
      <aside className="planning-stream-sidebar">
        <PlanningProgressPanel events={planningEvents} currentDeviceName={currentDeviceName} running={Boolean(activeRunId)} stopping={stopPlanning.isPending} onStop={() => stopPlanning.mutate()} />
      </aside>
      </div>
      <Modal
        open={saveTemplateOpen}
        title="保存配置模板"
        okText="保存模板"
        cancelText="取消"
        confirmLoading={saveTemplate.isPending}
        okButtonProps={{ disabled: !templateTitle.trim() }}
        onCancel={() => setSaveTemplateOpen(false)}
        onOk={() => saveTemplate.mutate()}
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Input value={templateTitle} onChange={(event) => setTemplateTitle(event.target.value)} placeholder="模板标题，例如：三交换机双 VLAN 跨 VLAN 通信" />
          <Input.TextArea value={templateDescription} onChange={(event) => setTemplateDescription(event.target.value)} rows={3} placeholder="模板简介：说明适用的拓扑模式、业务目的或配置注意事项" />
          <Typography.Text type="secondary">保存后固定记录当前拓扑、需求、配置思路与命令。后续新任务只会把它传给 LLM 作参考，不能直接套用旧参数。</Typography.Text>
        </Space>
      </Modal>
    </>
  );
}

function TaskReview({ task, selectedPlanId, onSelectPlan, savePlan, saving, onSaveTemplate }: { task: ConfigTask; selectedPlanId: string | null; onSelectPlan: (planId: string) => void; savePlan: (planId: string, revision: number, commands: string[]) => void; saving: boolean; onSaveTemplate: () => void }) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const selected = task.device_plans.find((item) => item.id === selectedPlanId) || task.device_plans[0];
  useEffect(() => {
    if (selected && selected.id !== selectedPlanId) onSelectPlan(selected.id);
  }, [selected, selectedPlanId, onSelectPlan]);
  if (!selected) return null;
  const commandText = drafts[selected.id] ?? selected.commands.join("\n");
  return (
    <Card size="small" title={`任务 ${task.id.slice(0, 8)}`} extra={<Button icon={<SaveOutlined />} onClick={onSaveTemplate}>保存为模板</Button>}>
      <Descriptions size="small" column={2}><Descriptions.Item label="状态"><Tag color={task.status === "blocked" ? "warning" : "processing"}>{task.status}</Tag></Descriptions.Item><Descriptions.Item label="规划提示">{task.blocking_reason || "-"}</Descriptions.Item></Descriptions>
      <Space className="device-plan-tabs" wrap>
        {task.device_plans.map((plan) => <Button key={plan.id} type={plan.id === selected.id ? "primary" : "default"} onClick={() => onSelectPlan(plan.id)}>{plan.display_name}</Button>)}
      </Space>
      <Card className="device-plan-command-panel" size="small" style={{ marginTop: 12 }} title={selected.display_name} extra={<Tag color={selected.approved_at ? "success" : "processing"}>{selected.approved_at ? "已保存" : "待用户审阅"}</Tag>}>
        <Typography.Paragraph type="secondary">现场型号：{selected.detected_model || "未查询"}；系列：{selected.mapped_series || "未映射"}；手册状态：{selected.compatibility_status}</Typography.Paragraph>
        <Typography.Paragraph type="secondary"><b>模型命令计划：</b>{String((selected.intent.llm_command_plan as { status?: string } | undefined)?.status || "未调用")}</Typography.Paragraph>
        <Typography.Text strong>命令证据（每页 10 条）</Typography.Text>
        <List className="compact-list" size="small" pagination={{ pageSize: 10, size: "small", showSizeChanger: false, hideOnSinglePage: true }} dataSource={selected.evidence} renderItem={(e) => <List.Item><List.Item.Meta title={<Typography.Text code>{e.canonical_name}</Typography.Text>} description={e.source_path} /></List.Item>} />
        <Typography.Text strong>本设备配置命令（可直接修改）</Typography.Text>
        <Input.TextArea className="command-row" autoSize={{ minRows: 8, maxRows: 32 }} value={commandText} onChange={(event) => setDrafts((current) => ({ ...current, [selected.id]: event.target.value }))} placeholder="这里是模型根据手册检索生成的命令草案，用户可以自由修改。" disabled={Boolean(selected.approved_at)} />
        {Array.isArray(selected.validation.warnings) && selected.validation.warnings.length > 0 && <Alert type="info" showIcon style={{ marginTop: 8 }} message="生成提示" description={selected.validation.warnings.join("；")} />}
        {!selected.approved_at && <Button style={{ marginTop: 12 }} icon={<CheckOutlined />} loading={saving} disabled={!commandText.trim()} onClick={() => savePlan(selected.id, selected.approval_revision, commandText.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))}>保存本设备命令</Button>}
        {selected.approved_at && <Tag color="success" style={{ marginTop: 12 }}>已保存，可在下发页逐台使用</Tag>}
      </Card>
    </Card>
  );
}

function PlanningProgressPanel({ events, currentDeviceName, running, stopping, onStop }: { events: PlanningEvent[]; currentDeviceName: string | null; running: boolean; stopping: boolean; onStop: () => void }) {
  const [activeThinkingKeys, setActiveThinkingKeys] = useState<string[]>([]);
  const deviceEvents = currentDeviceName ? events.filter((item) => item.stage.startsWith(`${currentDeviceName} ·`)) : events;
  const displayStage = (stage: string) => currentDeviceName ? stage.replace(`${currentDeviceName} · `, "") : stage;
  const stageEvents = deviceEvents.filter((item) => item.event_type === "stage" || item.event_type === "done" || item.event_type === "cancelled" || item.event_type === "error");
  const groupByStage = (eventType: PlanningEvent["event_type"]) => {
    const grouped = new Map<string, string>();
    deviceEvents.filter((item) => item.event_type === eventType).forEach((item) => {
      grouped.set(item.stage, `${grouped.get(item.stage) || ""}${item.content}`);
    });
    return [...grouped.entries()];
  };
  const thinkingGroups = groupByStage("thinking");
  const outputGroups = groupByStage("output");
  useEffect(() => {
    // Keep only the newest active reasoning node open while it is streaming;
    // completed and cancelled runs return the sidebar to its compact state.
    setActiveThinkingKeys(running && thinkingGroups.length ? [thinkingGroups[thinkingGroups.length - 1][0]] : []);
  }, [running, thinkingGroups.length]);
  const timelineColor = (event: PlanningEvent) => {
    if (event.event_type === "error" || event.event_type === "cancelled") return "red";
    if (event.event_type === "done") return "green";
    return "blue";
  };
  return (
    <Card size="small" title={currentDeviceName ? `${currentDeviceName} · 配置生成` : "规划进度"} extra={running ? <Button danger size="small" icon={<StopOutlined />} loading={stopping} onClick={onStop}>停止</Button> : <Tag color="default">空闲</Tag>}>
      {running && <Space size={8} style={{ marginBottom: 12 }}><Spin size="small" /><Typography.Text type="secondary">任务正在异步执行</Typography.Text></Space>}
      <Typography.Text strong>当前阶段</Typography.Text>
      {stageEvents.length ? <Timeline className="planning-stage-timeline" items={stageEvents.map((event) => ({ color: timelineColor(event), children: <><Typography.Text strong={event.event_type === "stage"}>{displayStage(event.stage)}</Typography.Text><Typography.Paragraph type="secondary" style={{ margin: "2px 0 0" }}>{event.content}</Typography.Paragraph></> }))} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="开始配置任务后将在这里显示流程阶段" />}
      <Typography.Text strong>模型思考</Typography.Text>
      {thinkingGroups.length ? <Collapse className="planning-thinking-collapse" size="small" activeKey={activeThinkingKeys} onChange={(keys) => setActiveThinkingKeys(Array.isArray(keys) ? keys : [keys])} items={thinkingGroups.map(([stage, content], index) => ({ key: stage, label: running && index === thinkingGroups.length - 1 ? `${stage}（正在流式输出）` : `${stage}（思考已结束）`, children: <div className="planning-stream-text">{content}</div> }))} /> : <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>模型未返回可展示的 thinking 内容；正式输出仍会显示在下方。</Typography.Paragraph>}
      <Typography.Text strong>正式输出</Typography.Text>
      {outputGroups.length ? <div className="planning-output-scroll">{outputGroups.map(([stage, content]) => <div className="planning-output-block" key={stage}><Typography.Text type="secondary">{displayStage(stage)}</Typography.Text><div className="planning-stream-text">{content}</div></div>)}</div> : <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>等待当前设备输出。</Typography.Paragraph>}
    </Card>
  );
}
