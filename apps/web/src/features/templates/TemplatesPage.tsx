import { DeleteOutlined, DesktopOutlined, DownloadOutlined, EditOutlined, EyeOutlined, HddOutlined, UploadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MiniMap, Position, ReactFlow, getStraightPath, useEdgesState, useNodesState, type Edge, type EdgeProps, type Node, type NodeProps } from "@xyflow/react";
import { Button, Card, Collapse, Descriptions, Empty, Input, Modal, Popconfirm, Space, Table, Tag, Typography, Upload, message } from "antd";
import { useEffect, useState } from "react";
import { chooseDesktopExportPath, deleteTemplate, exportTemplate, getTemplate, importTemplate, listTemplates, saveTemplateExport, updateTemplate, type ConfigurationTemplateDetail, type ConfigurationTemplateSummary, type TopologyLink, type TopologyNode } from "../../api/client";

const formatTime = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });

type TemplateFlowNodeData = { label: string; kind: string };
type TemplateLinkData = { sourcePort?: string; targetPort?: string };
type TemplateFlowEdge = Edge<TemplateLinkData, "templateInterface">;

function TemplateDeviceNode({ data }: NodeProps<Node<TemplateFlowNodeData>>) {
  const isSwitch = data.kind === "switch";
  return <div className={`template-flow-node ${isSwitch ? "is-switch" : "is-pc"}`}>
    <Handle className="topology-hidden-handle" type="target" position={Position.Left} id="hidden-target" style={{ left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} />
    <Handle className="topology-hidden-handle" type="source" position={Position.Right} id="hidden-source" style={{ left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} />
    {isSwitch ? <HddOutlined /> : <DesktopOutlined />}
    <span>{data.label}</span>
  </div>;
}

function TemplateInterfaceEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps<TemplateFlowEdge>) {
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  const sourceLabelX = sourceX + (targetX - sourceX) * 0.22;
  const sourceLabelY = sourceY + (targetY - sourceY) * 0.22;
  const targetLabelX = sourceX + (targetX - sourceX) * 0.78;
  const targetLabelY = sourceY + (targetY - sourceY) * 0.78;
  return <>
    <BaseEdge id={id} path={edgePath} />
    <EdgeLabelRenderer>
      <div className="interface-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${sourceLabelX}px,${sourceLabelY}px)` }}>
        {data?.sourcePort || "未填写接口"}
      </div>
      <div className="interface-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${targetLabelX}px,${targetLabelY}px)` }}>
        {data?.targetPort || "未填写接口"}
      </div>
    </EdgeLabelRenderer>
  </>;
}

const templateNodeTypes = { templateDevice: TemplateDeviceNode };
const templateEdgeTypes = { templateInterface: TemplateInterfaceEdge };

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function TemplatesPage() {
  const queryClient = useQueryClient();
  const templates = useQuery({ queryKey: ["templates"], queryFn: listTemplates });
  const [detail, setDetail] = useState<ConfigurationTemplateDetail | null>(null);
  const [editing, setEditing] = useState<ConfigurationTemplateSummary | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const loadDetail = useMutation({
    mutationFn: getTemplate,
    onSuccess: setDetail,
    onError: () => message.error("模板详情加载失败。")
  });
  const saveEdit = useMutation({
    mutationFn: () => editing ? updateTemplate(editing.id, { title, description }) : Promise.reject(new Error("模板不存在")),
    onSuccess: () => {
      setEditing(null);
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      message.success("模板信息已保存。");
    },
    onError: () => message.error("模板信息保存失败。")
  });
  const remove = useMutation({
    mutationFn: deleteTemplate,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      message.success("模板已删除。");
    },
    onError: () => message.error("模板删除失败。")
  });
  const exportArchive = useMutation({
    mutationFn: async (id: string) => {
      const filename = `template-${id.slice(0, 8)}.template.json`;
      const destinationPath = await chooseDesktopExportPath(filename, "template");
      if (destinationPath === null) return { kind: "cancelled" } as const;
      if (destinationPath) {
        return { kind: "saved", savedPath: (await saveTemplateExport(id, destinationPath)).saved_path } as const;
      }
      return { kind: "download", file: await exportTemplate(id), filename } as const;
    },
    onSuccess: (result) => {
      if (result.kind === "cancelled") return;
      if (result.kind === "saved") {
        message.success({ content: `已导出到：${result.savedPath}`, duration: 8 });
        return;
      }
      downloadBlob(result.file.blob, result.filename);
      message.success({
        content: result.file.saved_path ? `已导出到：${result.file.saved_path}` : "已触发浏览器下载，请查看默认下载目录。",
        duration: 8,
      });
    },
    onError: () => message.error("模板导出失败。"),
  });
  const importArchive = useMutation({
    mutationFn: ({ file, overwrite }: { file: File; overwrite?: boolean }) => importTemplate(file, overwrite),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      message.success("模板归档已导入。");
    },
    onError: (error, variables) => {
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status === 409 && !variables.overwrite) {
        Modal.confirm({
          title: "发现同名模板",
          content: "是否覆盖已有模板？覆盖只更新该模板快照，不影响来源任务。",
          okText: "覆盖导入",
          cancelText: "取消",
          onOk: () => importArchive.mutate({ file: variables.file, overwrite: true }),
        });
        return;
      }
      message.error("模板归档导入失败。" );
    },
  });
  const beginEdit = (item: ConfigurationTemplateSummary) => {
    setEditing(item);
    setTitle(item.title);
    setDescription(item.description);
  };

  return (
    <>
      <Typography.Title level={2} className="page-title">模板管理</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">这里保存经你审阅的配置结果快照，用于查看、导入导出与人工复用；模板不会参与新任务的命令生成。</Typography.Text>
      <Space style={{ margin: "16px 0" }} wrap>
        <Upload accept=".json" showUploadList={false} beforeUpload={(file) => { importArchive.mutate({ file }); return false; }}>
          <Button icon={<UploadOutlined />} loading={importArchive.isPending}>导入模板</Button>
        </Upload>
      </Space>
      <Card>
        <Table
          rowKey="id"
          loading={templates.isLoading}
          dataSource={templates.data || []}
          locale={{ emptyText: <Empty description="还没有配置模板。在配置规划页生成设备命令后，可选择“保存为模板”。" /> }}
          pagination={{ pageSize: 10 }}
          scroll={{ x: 980 }}
          columns={[
            { title: "标题", dataIndex: "title", width: 240, ellipsis: true },
            { title: "简介", dataIndex: "description", ellipsis: true, render: (value) => value || "-" },
            { title: "来源手册", dataIndex: "manual_name", width: 220, ellipsis: true, render: (value) => value || "未记录" },
            { title: "设备命令集", dataIndex: "device_plan_count", width: 108, render: (value) => `${value} 台` },
            { title: "更新时间", dataIndex: "updated_at", width: 170, render: formatTime },
            {
              title: "操作",
              width: 205,
              render: (_, row: ConfigurationTemplateSummary) => <Space size={4} wrap>
                <Button size="small" icon={<EyeOutlined />} loading={loadDetail.isPending} onClick={() => loadDetail.mutate(row.id)}>查看</Button>
                <Button size="small" icon={<EditOutlined />} onClick={() => beginEdit(row)}>编辑</Button>
                <Button size="small" icon={<DownloadOutlined />} loading={exportArchive.isPending} onClick={() => exportArchive.mutate(row.id)}>导出</Button>
                <Popconfirm title="删除该配置模板？" description="删除后不可恢复，不影响来源任务。" okText="删除" cancelText="取消" onConfirm={() => remove.mutate(row.id)}>
                  <Button size="small" danger icon={<DeleteOutlined />} loading={remove.isPending}>删除</Button>
                </Popconfirm>
              </Space>
            }
          ]}
        />
      </Card>
      <Modal open={Boolean(detail)} title={detail ? `模板详情：${detail.title}` : "模板详情"} width={1040} footer={<Button onClick={() => setDetail(null)}>关闭</Button>} onCancel={() => setDetail(null)}>
        {detail && <TemplateDetailView detail={detail} />}
      </Modal>
      <Modal open={Boolean(editing)} title="编辑模板" okText="保存" cancelText="取消" confirmLoading={saveEdit.isPending} okButtonProps={{ disabled: !title.trim() }} onOk={() => saveEdit.mutate()} onCancel={() => setEditing(null)}>
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="模板标题" />
          <Input.TextArea value={description} onChange={(event) => setDescription(event.target.value)} rows={4} placeholder="模板简介：适用拓扑、业务目标或配置注意事项" />
        </Space>
      </Modal>
    </>
  );
}

function TemplateDetailView({ detail }: { detail: ConfigurationTemplateDetail }) {
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Descriptions bordered size="small" column={2}>
        <Descriptions.Item label="来源手册">{detail.manual_name || "未记录"}</Descriptions.Item>
        <Descriptions.Item label="保存时间">{formatTime(detail.created_at)}</Descriptions.Item>
        <Descriptions.Item label="简介" span={2}>{detail.description || "-"}</Descriptions.Item>
      </Descriptions>
      <Card size="small" title="拓扑快照">
        <TemplateTopologyPreview nodes={detail.topology.nodes} links={detail.topology.links} />
      </Card>
      <Card size="small" title="配置要求"><Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{detail.requirement_text || "-"}</Typography.Paragraph></Card>
      <Card size="small" title="配置思路"><Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{detail.planning_idea || "-"}</Typography.Paragraph></Card>
      <Card size="small" title="分设备配置命令">
        <Collapse items={detail.device_plans.map((plan) => ({
          key: plan.device_node_id,
          label: plan.display_name,
          children: <Typography.Paragraph className="command-preview">{plan.commands.join("\n") || "未生成命令"}</Typography.Paragraph>
        }))} />
      </Card>
    </Space>
  );
}

function TemplateTopologyPreview({ nodes, links }: { nodes: TopologyNode[]; links: TopologyLink[] }) {
  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<Node<TemplateFlowNodeData>>([]);
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState<TemplateFlowEdge>([]);
  useEffect(() => {
    setFlowNodes(nodes.map((node, index) => ({
      id: node.id,
      type: "templateDevice",
      position: {
        x: Number.isFinite(node.x) ? node.x : 100 + index * 180,
        y: Number.isFinite(node.y) ? node.y : 100 + index * 90,
      },
      data: { label: node.name || node.id, kind: node.kind },
    })));
    setFlowEdges(links.map((link) => ({
      id: link.id,
      source: link.source,
      target: link.target,
      sourceHandle: "hidden-source",
      targetHandle: "hidden-target",
      type: "templateInterface",
      data: { sourcePort: link.source_port, targetPort: link.target_port },
    })));
  }, [links, nodes, setFlowEdges, setFlowNodes]);

  if (!nodes.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该模板没有保存拓扑图" />;

  return <div className="template-topology-canvas" aria-label="已保存的可拖动拓扑图">
    <ReactFlow
      nodes={flowNodes}
      edges={flowEdges}
      nodeTypes={templateNodeTypes}
      edgeTypes={templateEdgeTypes}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodesConnectable={false}
      nodesDraggable
      elementsSelectable={false}
      fitView
    >
      <Background gap={18} size={1} />
      <Controls />
      <MiniMap />
    </ReactFlow>
  </div>;
}
