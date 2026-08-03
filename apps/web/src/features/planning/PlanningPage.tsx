import { CheckOutlined, PlayCircleOutlined, SearchOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Input, List, Select, Space, Tag, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { approveDevicePlan, createConfigTask, listManuals, searchCommands, type CommandHit, type ConfigTask } from "../../api/client";

export function PlanningPage() {
  const [query, setQuery] = useState("vlan batch");
  const [revisionId, setRevisionId] = useState("");
  const [manualId, setManualId] = useState("");
  const [requirement, setRequirement] = useState("创建 VLAN 10，并将交换机接入口配置为 Access 加入 VLAN 10。");
  const [task, setTask] = useState<ConfigTask | null>(null);
  const manuals = useQuery({ queryKey: ["manuals"], queryFn: listManuals });
  useEffect(() => setRevisionId(localStorage.getItem("last-topology-revision") || ""), []);
  const search = useMutation({ mutationFn: (value: string) => searchCommands(value) });
  const createTask = useMutation({
    mutationFn: createConfigTask,
    onSuccess: (result) => { setTask(result); message.success(result.status === "blocked" ? "任务已创建，但安全门禁已阻断写执行。" : "任务已创建，等待审阅。"); },
    onError: () => message.error("创建任务失败；请先从拓扑页保存 revision，并选择已完成抽取的手册。")
  });
  const approve = useMutation({
    mutationFn: ({ planId, revision }: { planId: string; revision: number }) => {
      if (!task) return Promise.reject(new Error("任务不存在"));
      return approveDevicePlan(task.id, planId, { approval_revision: revision });
    },
    onSuccess: (updated) => {
      setTask((current) => current ? { ...current, device_plans: current.device_plans.map((item) => item.id === updated.id ? updated : item) } : current);
      message.success("该设备命令集已审批；写入执行仍未开放。");
    },
    onError: () => message.error("审批未通过；请检查型号、版本、端口和静态验证。")
  });
  const submit = () => { if (query.trim()) search.mutate(query.trim()); };
  return (
    <>
      <Typography.Title level={2} className="page-title">配置规划</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">本页生成受手册证据、发布型号、版本和拓扑端口映射约束的逐设备规划；审批不等于下发。</Typography.Text>
      <Alert type="info" showIcon message="安全门禁" description="检索结果不是可执行配置；后续生成必须通过型号/版本过滤、视图/参数校验并逐台人工确认。" style={{ marginBottom: 16 }} />
      <Space.Compact style={{ width: "100%", maxWidth: 700, marginBottom: 16 }}>
        <Input value={query} onChange={(event) => setQuery(event.target.value)} onPressEnter={submit} placeholder="例如：vlan batch、port link-type、display version" />
        <Input.Search enterButton={<SearchOutlined />} loading={search.isPending} onSearch={submit} />
      </Space.Compact>
      <Card title="手册命令证据">
        <List
          loading={search.isPending}
          dataSource={search.data || []}
          locale={{ emptyText: "输入关键词后查询已注入手册" }}
          renderItem={(item: CommandHit) => <List.Item><List.Item.Meta title={<Space><Typography.Text code>{item.canonical_name}</Typography.Text><Tag>{item.feature || "未分类"}</Tag><Tag color="blue">{item.applicability_mode}</Tag>{item.retrieval_sources.map((source) => <Tag key={source} color="purple">{source}</Tag>)}</Space>} description={<div><div className="command-row">{item.syntax.join("\n")}</div><Typography.Text type="secondary">来源：{item.source_path}；混合分数：{item.score?.toFixed(3) ?? "-"}</Typography.Text></div>} /></List.Item>}
        />
      </Card>
      <Card title="创建配置任务" style={{ marginTop: 16 }} extra={<Tag color="gold">写入执行尚未开放</Tag>}>
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Input value={revisionId} onChange={(event) => setRevisionId(event.target.value)} placeholder="粘贴拓扑页保存后返回的 revision ID" />
          <Select value={manualId || undefined} onChange={setManualId} placeholder="选择已完成抽取的手册" options={(manuals.data || []).filter((item) => item.status.startsWith("completed")).map((item) => ({ value: item.id, label: `${item.brand || "未知"} · ${item.original_filename} · ${item.release || "未标注"}` }))} />
          <Input.TextArea value={requirement} onChange={(event) => setRequirement(event.target.value)} rows={3} />
          <Button type="primary" icon={<PlayCircleOutlined />} loading={createTask.isPending} disabled={!revisionId || !manualId} onClick={() => createTask.mutate({ topology_revision_id: revisionId, manual_id: manualId, requirement_text: requirement })}>生成受限规划</Button>
          {task && <TaskReview task={task} approve={(planId, revision) => approve.mutate({ planId, revision })} approving={approve.isPending} />}
        </Space>
      </Card>
    </>
  );
}

function TaskReview({ task, approve, approving }: { task: ConfigTask; approve: (planId: string, revision: number) => void; approving: boolean }) {
  return (
    <Card size="small" title={`任务 ${task.id.slice(0, 8)}`}>
      <Descriptions size="small" column={2}><Descriptions.Item label="状态"><Tag color={task.status === "blocked" ? "error" : "processing"}>{task.status}</Tag></Descriptions.Item><Descriptions.Item label="阻断原因">{task.blocking_reason || "-"}</Descriptions.Item></Descriptions>
      {task.device_plans.map((plan) => <Card key={plan.id} size="small" style={{ marginTop: 12 }} title={plan.display_name} extra={<Tag color={plan.compatibility_status === "exact" ? "success" : "error"}>{plan.compatibility_status}</Tag>}>
        <Typography.Paragraph type="secondary">型号：{plan.detected_model || "未确认"}；版本：{plan.detected_release || "未确认"}；系列：{plan.mapped_series || "未映射"}</Typography.Paragraph>
        <Alert type={plan.compatibility_status === "exact" ? "success" : "error"} showIcon message={plan.compatibility_reason || "-"} />
        <Typography.Paragraph style={{ marginTop: 12 }}><b>规划意图：</b>{JSON.stringify(plan.intent)}</Typography.Paragraph>
        <Typography.Paragraph type="secondary"><b>LLM 命令计划：</b>{String((plan.intent.llm_command_plan as { status?: string } | undefined)?.status || "未调用")}；只有绑定手册证据、拓扑端口和确定性参数校验后才会生成下方 CLI。</Typography.Paragraph>
        {Object.keys(plan.command_plan).length > 0 && <Typography.Paragraph type="secondary"><b>CommandPlan：</b>{JSON.stringify(plan.command_plan)}</Typography.Paragraph>}
        <Typography.Text strong>命令证据（前 20 条）</Typography.Text>
        <List className="compact-list" size="small" dataSource={plan.evidence} renderItem={(e) => <List.Item><Typography.Text code>{e.canonical_name}</Typography.Text><Typography.Text type="secondary">{e.source_path}</Typography.Text></List.Item>} />
        <Typography.Text strong>待审批配置命令</Typography.Text>
        <div className="command-row">{plan.commands.length ? plan.commands.join("\n") : "已被安全门禁阻断，未生成写命令。"}</div>
        {plan.validation.source === "llm_command_plan_compiled" && <Tag color="geekblue" style={{ marginTop: 8 }}>LLM CommandPlan 已由确定性编译器生成</Tag>}
        {plan.validation.status === "ready" && !plan.approved_at && <Button style={{ marginTop: 12 }} icon={<CheckOutlined />} loading={approving} onClick={() => approve(plan.id, plan.approval_revision)}>确认本设备命令集</Button>}
        {plan.approved_at && <Tag color="success" style={{ marginTop: 12 }}>已审批，等待逐台下发模块</Tag>}
      </Card>)}
    </Card>
  );
}
