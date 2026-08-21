import { DeleteOutlined, DesktopOutlined, DownloadOutlined, EditOutlined, EyeOutlined, FileAddOutlined, HddOutlined, UploadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MiniMap, Position, ReactFlow, getStraightPath, useEdgesState, useNodesState, type Edge, type EdgeProps, type Node, type NodeProps } from "@xyflow/react";
import { Alert, Button, Card, Collapse, Descriptions, Empty, Input, Modal, Popconfirm, Select, Space, Spin, Table, Tabs, Tag, Typography, Upload, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { chooseDesktopExportPath, createTemplate, deleteTemplate, exportTemplate, getTemplate, getTopology, importTemplate, listTemplates, listTopologies, saveTemplateExport, updateTemplate, type ConfigurationTemplateDetail, type ConfigurationTemplateSummary, type SavedTopology, type TemplateSnapshotInput, type TopologyLink, type TopologyNode } from "../../api/client";

const formatTime = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });
type TemplateFlowNodeData = { label: string; kind: string };
type TemplateLinkData = { sourcePort?: string; targetPort?: string };
type TemplateFlowEdge = Edge<TemplateLinkData, "templateInterface">;
type TemplateGraph = { name: string; nodes: TopologyNode[]; links: TopologyLink[] };
type TemplateEditorState = { id?: string; title: string; description: string; topologyId?: string; topology?: TemplateGraph; requirementText: string; planningIdea: string; commandTextByDevice: Record<string, string> };

function TemplateDeviceNode({ data }: NodeProps<Node<TemplateFlowNodeData>>) {
  const isSwitch = data.kind === "switch";
  return <div className={`template-flow-node ${isSwitch ? "is-switch" : "is-pc"}`}>
    <Handle className="topology-hidden-handle" type="target" position={Position.Left} id="hidden-target" style={{ left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} />
    <Handle className="topology-hidden-handle" type="source" position={Position.Right} id="hidden-source" style={{ left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} />
    {isSwitch ? <HddOutlined /> : <DesktopOutlined />}<span>{data.label}</span>
  </div>;
}
function TemplateInterfaceEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps<TemplateFlowEdge>) {
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  const sourceLabelX = sourceX + (targetX - sourceX) * 0.22;
  const sourceLabelY = sourceY + (targetY - sourceY) * 0.22;
  const targetLabelX = sourceX + (targetX - sourceX) * 0.78;
  const targetLabelY = sourceY + (targetY - sourceY) * 0.78;
  return <><BaseEdge id={id} path={edgePath} /><EdgeLabelRenderer>
    <div className="interface-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${sourceLabelX}px,${sourceLabelY}px)` }}>{data?.sourcePort || "未填写接口"}</div>
    <div className="interface-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${targetLabelX}px,${targetLabelY}px)` }}>{data?.targetPort || "未填写接口"}</div>
  </EdgeLabelRenderer></>;
}
const templateNodeTypes = { templateDevice: TemplateDeviceNode };
const templateEdgeTypes = { templateInterface: TemplateInterfaceEdge };

function downloadBlob(blob: Blob, filename: string) { const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename; anchor.click(); URL.revokeObjectURL(url); }
function editorFromDetail(detail: ConfigurationTemplateDetail): TemplateEditorState {
  return { id: detail.id, title: detail.title, description: detail.description, topologyId: detail.topology_id || undefined, topology: detail.topology, requirementText: detail.requirement_text, planningIdea: detail.planning_idea, commandTextByDevice: Object.fromEntries(detail.device_plans.map((plan) => [plan.device_node_id, plan.commands.join("\n")])) };
}
function buildSnapshot(editor: TemplateEditorState): TemplateSnapshotInput | null {
  if (!editor.topology) return null;
  return { topology: editor.topology, topology_id: editor.topologyId || null, requirement_text: editor.requirementText, planning_idea: editor.planningIdea, device_plans: editor.topology.nodes.filter((node) => node.kind === "switch").map((node) => ({ display_name: node.name || node.id, device_node_id: node.id, commands: (editor.commandTextByDevice[node.id] || "").split(/\r?\n/).map((line) => line.trimEnd()).filter(Boolean) })) };
}

export function TemplatesPage() {
  const queryClient = useQueryClient();
  const templates = useQuery({ queryKey: ["templates"], queryFn: listTemplates });
  const topologies = useQuery({ queryKey: ["topologies"], queryFn: listTopologies });
  const [detail, setDetail] = useState<ConfigurationTemplateDetail | null>(null);
  const [editor, setEditor] = useState<TemplateEditorState | null>(null);
  const loadDetail = useMutation({ mutationFn: getTemplate, onSuccess: setDetail, onError: () => message.error("模板详情加载失败。") });
  const loadForEdit = useMutation({ mutationFn: getTemplate, onSuccess: (value) => setEditor(editorFromDetail(value)), onError: () => message.error("模板编辑内容加载失败。") });
  const loadTopology = useMutation({ mutationFn: getTopology });
  const saveEditor = useMutation({
    mutationFn: async () => {
      if (!editor) throw new Error("模板编辑器未打开");
      const snapshot = buildSnapshot(editor);
      if (!editor.title.trim()) throw new Error("请填写模板名称");
      if (!snapshot) throw new Error("请选择已有拓扑");
      return editor.id ? updateTemplate(editor.id, { title: editor.title, description: editor.description, snapshot }) : createTemplate({ title: editor.title, description: editor.description, snapshot });
    },
    onSuccess: () => { setEditor(null); queryClient.invalidateQueries({ queryKey: ["templates"] }); message.success("模板已保存。保存的是独立拓扑快照，后续修改原拓扑不会影响它。"); },
    onError: (error) => message.error(error instanceof Error ? error.message : "模板保存失败。"),
  });
  const remove = useMutation({ mutationFn: deleteTemplate, onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["templates"] }); message.success("模板已删除。"); }, onError: () => message.error("模板删除失败。") });
  const exportArchive = useMutation({
    mutationFn: async (id: string) => { const filename = `template-${id.slice(0, 8)}.template.json`; const destinationPath = await chooseDesktopExportPath(filename, "template"); if (destinationPath === null) return { kind: "cancelled" } as const; if (destinationPath) return { kind: "saved", savedPath: (await saveTemplateExport(id, destinationPath)).saved_path } as const; return { kind: "download", file: await exportTemplate(id), filename } as const; },
    onSuccess: (result) => { if (result.kind === "cancelled") return; if (result.kind === "saved") { message.success({ content: `已导出到：${result.savedPath}`, duration: 8 }); return; } downloadBlob(result.file.blob, result.filename); message.success({ content: result.file.saved_path ? `已导出到：${result.file.saved_path}` : "已触发浏览器下载，请查看默认下载目录。", duration: 8 }); },
    onError: () => message.error("模板导出失败。"),
  });
  const importArchive = useMutation({
    mutationFn: ({ file, overwrite }: { file: File; overwrite?: boolean }) => importTemplate(file, overwrite),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["templates"] }); message.success("模板归档已导入。"); },
    onError: (error, variables) => { const status = (error as { response?: { status?: number } })?.response?.status; if (status === 409 && !variables.overwrite) { Modal.confirm({ title: "发现同名模板", content: "是否覆盖已有模板？覆盖会更新该模板的完整快照。", okText: "覆盖导入", cancelText: "取消", onOk: () => importArchive.mutate({ file: variables.file, overwrite: true }) }); return; } message.error("模板归档导入失败。"); },
  });
  const startCreate = () => setEditor({ title: "", description: "", requirementText: "", planningIdea: "", commandTextByDevice: {} });
  const selectTopology = (topologyId: string) => {
    const apply = (topology: SavedTopology) => setEditor((current) => current ? { ...current, topologyId: topology.id, topology: topology.graph, commandTextByDevice: Object.fromEntries(topology.graph.nodes.filter((node) => node.kind === "switch").map((node) => [node.id, current.commandTextByDevice[node.id] || ""])) } : current);
    const load = () => loadTopology.mutate(topologyId, { onSuccess: apply, onError: () => message.error("拓扑加载失败。") });
    if (Object.values(editor?.commandTextByDevice || {}).some((value) => value.trim())) Modal.confirm({ title: "替换模板拓扑", content: "保留同一设备 ID 的命令；已不存在设备的命令将移除，新设备命令为空。是否继续？", okText: "替换拓扑", cancelText: "取消", onOk: load }); else load();
  };
  return <>
    <Typography.Title level={2} className="page-title">模板管理</Typography.Title>
    <Typography.Text type="secondary" className="page-subtitle">创建可独立维护的拓扑、需求、配置思路与分设备命令模板；模板保存的是快照，可作为后续规划的参考。</Typography.Text>
    <Space style={{ margin: "16px 0" }} wrap><Button type="primary" icon={<FileAddOutlined />} onClick={startCreate}>新建模板</Button><Upload accept=".json" showUploadList={false} beforeUpload={(file) => { importArchive.mutate({ file }); return false; }}><Button icon={<UploadOutlined />} loading={importArchive.isPending}>导入模板</Button></Upload></Space>
    <Card><Table rowKey="id" loading={templates.isLoading} dataSource={templates.data || []} locale={{ emptyText: <Empty description="还没有配置模板。可新建模板，或从配置规划页保存当前结果。" /> }} pagination={{ pageSize: 10 }} scroll={{ x: 980 }} columns={[
      { title: "标题", dataIndex: "title", width: 240, ellipsis: true }, { title: "简介", dataIndex: "description", ellipsis: true, render: (value) => value || "-" }, { title: "来源手册", dataIndex: "manual_name", width: 220, ellipsis: true, render: (value) => value || "未记录" }, { title: "设备命令集", dataIndex: "device_plan_count", width: 108, render: (value) => `${value} 台` }, { title: "更新时间", dataIndex: "updated_at", width: 170, render: formatTime },
      { title: "操作", width: 230, render: (_, row: ConfigurationTemplateSummary) => <Space size={4} wrap><Button size="small" icon={<EyeOutlined />} loading={loadDetail.isPending} onClick={() => loadDetail.mutate(row.id)}>查看</Button><Button size="small" icon={<EditOutlined />} loading={loadForEdit.isPending} onClick={() => loadForEdit.mutate(row.id)}>编辑</Button><Button size="small" icon={<DownloadOutlined />} loading={exportArchive.isPending} onClick={() => exportArchive.mutate(row.id)}>导出</Button><Popconfirm title="删除该配置模板？" description="删除后不可恢复，不影响来源任务。" okText="删除" cancelText="取消" onConfirm={() => remove.mutate(row.id)}><Button size="small" danger icon={<DeleteOutlined />} loading={remove.isPending}>删除</Button></Popconfirm></Space> },
    ]} /></Card>
    <Modal open={Boolean(detail)} title={detail ? `模板详情：${detail.title}` : "模板详情"} width={1040} footer={<Button onClick={() => setDetail(null)}>关闭</Button>} onCancel={() => setDetail(null)}>{detail && <TemplateDetailView detail={detail} />}</Modal>
    <TemplateEditor editor={editor} topologies={topologies.data || []} topologyLoading={loadTopology.isPending} saving={saveEditor.isPending} onClose={() => setEditor(null)} onSave={() => saveEditor.mutate()} onSelectTopology={selectTopology} onChange={(updater) => setEditor((current) => current ? updater(current) : current)} />
  </>;
}

function TemplateEditor({ editor, topologies, topologyLoading, saving, onClose, onSave, onSelectTopology, onChange }: { editor: TemplateEditorState | null; topologies: Array<{ id: string; name: string }>; topologyLoading: boolean; saving: boolean; onClose: () => void; onSave: () => void; onSelectTopology: (id: string) => void; onChange: (updater: (current: TemplateEditorState) => TemplateEditorState) => void }) {
  const switches = useMemo(() => (editor?.topology?.nodes || []).filter((node) => node.kind === "switch"), [editor?.topology]);
  const tabItems = switches.map((node) => ({ key: node.id, label: <span>{node.name || node.id}{editor?.commandTextByDevice[node.id]?.trim() ? "" : "（未填写）"}</span>, children: <Input.TextArea className="command-row" rows={14} value={editor?.commandTextByDevice[node.id] || ""} placeholder={`填写 ${node.name || node.id} 的配置命令，每行一条。PC 不需要命令。`} onChange={(event) => onChange((current) => ({ ...current, commandTextByDevice: { ...current.commandTextByDevice, [node.id]: event.target.value } }))} /> }));
  return <Modal open={Boolean(editor)} title={editor?.id ? "编辑配置模板" : "新建配置模板"} width={1220} style={{ top: 28 }} okText="保存模板" cancelText="取消" confirmLoading={saving} okButtonProps={{ disabled: !editor?.title.trim() || !editor?.topology }} onCancel={onClose} onOk={onSave} destroyOnClose>{editor && <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <Input value={editor.title} placeholder="模板名称，例如：双核心静态路由冗余" onChange={(event) => onChange((current) => ({ ...current, title: event.target.value }))} />
    <Input.TextArea value={editor.description} rows={2} placeholder="模板简介：适用场景、业务目的或人工审阅注意事项（可选）" onChange={(event) => onChange((current) => ({ ...current, description: event.target.value }))} />
    <Select value={editor.topologyId} placeholder={editor.topology ? "当前保存的是历史拓扑快照；可选择已有拓扑替换" : "选择系统中已有拓扑"} loading={topologyLoading} showSearch optionFilterProp="label" options={topologies.map((item) => ({ value: item.id, label: item.name }))} onChange={onSelectTopology} />
    {!editor.topology ? <Alert type="info" showIcon message="先选择一个已有拓扑" description="选择后会按其中的交换机建立命令编辑 Tab；拓扑图、需求、思路和命令将一起保存为模板快照。" /> : <><div className="template-editor-grid"><div><Typography.Text strong>拓扑快照</Typography.Text><TemplateTopologyPreview nodes={editor.topology.nodes} links={editor.topology.links} /></div><Space direction="vertical" size="middle" style={{ width: "100%" }}><div><Typography.Text strong>配置需求</Typography.Text><Input.TextArea rows={6} value={editor.requirementText} placeholder="填写该模板要实现的网络配置需求" onChange={(event) => onChange((current) => ({ ...current, requirementText: event.target.value }))} /></div><div><Typography.Text strong>配置思路</Typography.Text><Input.TextArea rows={6} value={editor.planningIdea} placeholder="填写配置的规划思路、步骤、约束和待确认项" onChange={(event) => onChange((current) => ({ ...current, planningIdea: event.target.value }))} /></div></Space></div><div><Typography.Text strong>设备配置命令</Typography.Text><Typography.Text type="secondary" style={{ marginLeft: 8 }}>按设备分别维护；只有交换机展示命令编辑器。</Typography.Text>{tabItems.length ? <Tabs items={tabItems} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="所选拓扑没有交换机，PC 不需要配置命令。" />}</div></>}
  </Space>}</Modal>;
}
function TemplateDetailView({ detail }: { detail: ConfigurationTemplateDetail }) { return <Space direction="vertical" size="middle" style={{ width: "100%" }}><Descriptions bordered size="small" column={2}><Descriptions.Item label="来源手册">{detail.manual_name || "未记录"}</Descriptions.Item><Descriptions.Item label="保存时间">{formatTime(detail.created_at)}</Descriptions.Item><Descriptions.Item label="简介" span={2}>{detail.description || "-"}</Descriptions.Item></Descriptions><Card size="small" title="拓扑快照"><TemplateTopologyPreview nodes={detail.topology.nodes} links={detail.topology.links} /></Card><Card size="small" title="配置要求"><Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{detail.requirement_text || "-"}</Typography.Paragraph></Card><Card size="small" title="配置思路"><Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{detail.planning_idea || "-"}</Typography.Paragraph></Card><Card size="small" title="分设备配置命令"><Collapse items={detail.device_plans.map((plan) => ({ key: plan.device_node_id, label: plan.display_name, children: <Typography.Paragraph className="command-preview">{plan.commands.join("\n") || "未生成命令"}</Typography.Paragraph> }))} /></Card></Space>; }
function TemplateTopologyPreview({ nodes, links }: { nodes: TopologyNode[]; links: TopologyLink[] }) { const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<Node<TemplateFlowNodeData>>([]); const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState<TemplateFlowEdge>([]); useEffect(() => { setFlowNodes(nodes.map((node, index) => ({ id: node.id, type: "templateDevice", position: { x: Number.isFinite(node.x) ? node.x : 100 + index * 180, y: Number.isFinite(node.y) ? node.y : 100 + index * 90 }, data: { label: node.name || node.id, kind: node.kind } }))); setFlowEdges(links.map((link) => ({ id: link.id, source: link.source, target: link.target, sourceHandle: "hidden-source", targetHandle: "hidden-target", type: "templateInterface", data: { sourcePort: link.source_port, targetPort: link.target_port } }))); }, [links, nodes, setFlowEdges, setFlowNodes]); if (!nodes.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该模板没有保存拓扑图" />; return <div className="template-topology-canvas" aria-label="已保存的可拖动拓扑图"><ReactFlow nodes={flowNodes} edges={flowEdges} nodeTypes={templateNodeTypes} edgeTypes={templateEdgeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} nodesConnectable={false} nodesDraggable elementsSelectable={false} fitView><Background gap={18} size={1} /><Controls /><MiniMap /></ReactFlow></div>; }
