import { DesktopOutlined, PlusOutlined, SafetyCertificateOutlined, SaveOutlined, SwitcherOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Background, Controls, Handle, MiniMap, Position, ReactFlow, addEdge, useEdgesState, useNodesState, type Connection, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { Alert, Button, Card, Empty, Form, Input, InputNumber, List, Modal, Select, Space, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { huaweiReadOnlyProbe, listManuals, listModels, saveTopology, type TopologyNode } from "../../api/client";

type TopologyData = { label: string; kind: "switch" | "pc"; modelId?: string; ip?: string; prefix?: number; gateway?: string; sshHost?: string; sshPort?: number; sshUsername?: string; detectedModel?: string; detectedRelease?: string; protectedPortsText?: string };

function DeviceNode({ data }: NodeProps<Node<TopologyData>>) {
  return (
    <div className={data.kind === "switch" ? "switch-node" : "pc-node"}>
      <Handle type="target" position={Position.Left} />
      {data.kind === "switch" ? <SwitcherOutlined /> : <DesktopOutlined />} {data.label}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { device: DeviceNode };

export function TopologyPage() {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models", "published"], queryFn: () => listModels(true) });
  const manuals = useQuery({ queryKey: ["manuals"], queryFn: listManuals });
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<TopologyData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const selected = nodes.find((node) => node.id === selectedId);
  const [form] = Form.useForm<TopologyData>();
  const [probeOpen, setProbeOpen] = useState(false);
  const [probeForm] = Form.useForm<{ host: string; port: number; username: string; password: string }>();
  const saveMutation = useMutation({
    mutationFn: () => saveTopology({
      name: "当前拓扑",
      nodes: nodes.map((node): TopologyNode => ({
        id: node.id, kind: node.data.kind, name: node.data.label, x: node.position.x, y: node.position.y,
        model_id: node.data.modelId, ip: node.data.ip, prefix: node.data.prefix, gateway: node.data.gateway,
        ssh_host: node.data.sshHost, ssh_port: node.data.sshPort, ssh_username: node.data.sshUsername,
        detected_model: node.data.detectedModel, detected_release: node.data.detectedRelease,
        protected_ports: (node.data.protectedPortsText || "").split(",").map((item) => item.trim()).filter(Boolean)
      })),
      links: edges.map((edge) => ({ id: edge.id, source: edge.source, source_port: String(edge.data?.sourcePort || "UNMAPPED"), target: edge.target, target_port: String(edge.data?.targetPort || "UNMAPPED") }))
    }),
    onSuccess: (result) => { localStorage.setItem("last-topology-revision", result.revision_id); message.success(`已保存本地拓扑 revision：${result.revision_id.slice(0, 8)}`); },
    onError: () => message.error("拓扑保存失败。")
  });
  const probeMutation = useMutation({
    mutationFn: huaweiReadOnlyProbe,
    onSuccess: (result) => {
      if (!selected) return;
      setNodes((current) => current.map((node) => node.id === selected.id ? { ...node, data: { ...node.data, detectedModel: result.detected_model || undefined, detectedRelease: result.detected_release || undefined } } : node));
      form.setFieldsValue({ detectedModel: result.detected_model || undefined, detectedRelease: result.detected_release || undefined });
      setProbeOpen(false);
      message.success(`只读识别完成：${result.detected_model || "未识别型号"} / ${result.detected_release || "未识别版本"}`);
    },
    onError: () => message.error("只读 SSH 探测失败；未发送任何配置命令。")
  });
  const modelOptions = useMemo(() => (models.data || []).filter((m) => m.level !== "series").map((m) => ({ value: m.id, label: `${m.brand} · ${m.canonical_name}` })), [models.data]);

  const addNode = (kind: "switch" | "pc") => {
    const id = `${kind}-${Date.now()}`;
    const next: Node<TopologyData> = {
      id,
      type: "device",
      position: { x: 100 + nodes.length * 45, y: 120 + nodes.length * 35 },
      data: { label: kind === "switch" ? `SW${nodes.filter((n) => n.data.kind === "switch").length + 1}` : `PC${nodes.filter((n) => n.data.kind === "pc").length + 1}`, kind }
    };
    setNodes((current) => [...current, next]);
    setSelectedId(id);
    form.setFieldsValue(next.data);
  };
  const onConnect = useCallback((connection: Connection) => setEdges((current) => addEdge({ ...connection, label: "待填写端口", data: { sourcePort: "", targetPort: "" } }, current)), [setEdges]);
  const updateSelected = (values: TopologyData) => {
    if (!selected) return;
    setNodes((current) => current.map((node) => node.id === selected.id ? { ...node, data: { ...node.data, ...values } } : node));
  };
  const selectNode = (_: React.MouseEvent, node: Node<TopologyData>) => { setSelectedId(node.id); form.setFieldsValue(node.data); };

  return (
    <>
      <Typography.Title level={2} className="page-title">拓扑编辑</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">拖入交换机或 PC，连线后填写端口。PC 默认无 SSH 字段；交换机只能选已发布型号。</Typography.Text>
      <div className="topology-layout">
        <Card className="panel-card" title="图元">
          <div className="node-palette">
            <Button icon={<SwitcherOutlined />} onClick={() => addNode("switch")}>添加交换机</Button>
            <Button icon={<DesktopOutlined />} onClick={() => addNode("pc")}>添加 PC</Button>
          </div>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 18 }}>
            首版保存为浏览器草稿；后续配置任务会冻结为后端拓扑 revision。
          </Typography.Paragraph>
        </Card>
        <div className="topology-canvas">
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodeClick={selectNode} fitView>
            <Background gap={18} size={1} />
            <Controls />
            <MiniMap />
          </ReactFlow>
        </div>
        <Card className="panel-card" title="设备属性" extra={<Tag>{selected?.data.kind || "未选择"}</Tag>}>
          {!selected ? <Empty description="选择一个设备节点" image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
            <Form form={form} layout="vertical" onValuesChange={(_, values) => updateSelected(values)}>
              <Form.Item name="label" label="名称"><Input /></Form.Item>
              <Form.Item name="ip" label="IP 地址"><Input placeholder="10.10.10.11" /></Form.Item>
              <Form.Item name="prefix" label="掩码前缀"><InputNumber min={0} max={128} style={{ width: "100%" }} /></Form.Item>
              <Form.Item name="gateway" label="网关"><Input /></Form.Item>
              {selected.data.kind === "switch" && <>
                <Form.Item name="modelId" label="型号"><Select showSearch options={modelOptions} placeholder="从已发布型号库选择" /></Form.Item>
                <Form.Item name="sshHost" label="SSH IP"><Input placeholder="192.168.56.2" /></Form.Item>
                <Form.Item name="sshPort" label="SSH 端口"><InputNumber min={1} max={65535} placeholder="22" style={{ width: "100%" }} /></Form.Item>
                <Form.Item name="sshUsername" label="SSH 用户"><Input placeholder="<USERNAME>" /></Form.Item>
                <Form.Item name="detectedModel" label="只读识别型号"><Input disabled /></Form.Item>
                <Form.Item name="detectedRelease" label="只读识别版本"><Input disabled /></Form.Item>
                <Form.Item name="protectedPortsText" label="受保护端口（逗号分隔）"><Input placeholder="例如：GE0/0/2" /></Form.Item>
                <Button icon={<SafetyCertificateOutlined />} onClick={() => { probeForm.setFieldsValue({ host: selected.data.sshHost, port: selected.data.sshPort || 22, username: selected.data.sshUsername }); setProbeOpen(true); }}>仅查询型号/版本</Button>
              </>}
            </Form>
          )}
        </Card>
      </div>
      {edges.length > 0 && <Card size="small" title="链路端口映射" style={{ marginTop: 16 }}>
        <Alert type="warning" showIcon message="只有填写端口名的交换机链路才会进入配置范围；未映射端口不会被规划器猜测。" style={{ marginBottom: 10 }} />
        <List dataSource={edges} renderItem={(edge) => <List.Item><Space wrap><Typography.Text>{edge.source} → {edge.target}</Typography.Text><Input value={String(edge.data?.sourcePort || "")} placeholder="源端口，例如 Ethernet0/0/1" onChange={(event) => setEdges((current) => current.map((item) => item.id === edge.id ? { ...item, data: { ...item.data, sourcePort: event.target.value } } : item))} /><Input value={String(edge.data?.targetPort || "")} placeholder="目标端口，例如 GE0/0/1" onChange={(event) => setEdges((current) => current.map((item) => item.id === edge.id ? { ...item, data: { ...item.data, targetPort: event.target.value } } : item))} /></Space></List.Item>} />
      </Card>}
      <Space style={{ marginTop: 16 }}><Button icon={<SaveOutlined />} loading={saveMutation.isPending} onClick={() => saveMutation.mutate()}>保存拓扑 revision</Button><Button icon={<PlusOutlined />} disabled={!manuals.data?.some((item) => item.status.startsWith("completed"))}>创建配置任务（请到配置规划页）</Button></Space>
      <Modal title="华为交换机只读型号识别" open={probeOpen} onCancel={() => setProbeOpen(false)} onOk={() => probeForm.validateFields().then((values) => probeMutation.mutate({ ...values, command: "display version" }))} confirmLoading={probeMutation.isPending} okText="执行 display version">
        <Alert type="info" showIcon message="只读白名单" description="仅发送 display version；不会进入 system-view，不会查询或修改 GE0/0/2，也不会保存配置。" style={{ marginBottom: 16 }} />
        <Form form={probeForm} layout="vertical"><Form.Item name="host" label="SSH IP" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="port" label="端口" rules={[{ required: true }]}><InputNumber min={1} max={65535} style={{ width: "100%" }} /></Form.Item><Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="password" label="密码" rules={[{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item></Form>
      </Modal>
    </>
  );
}
