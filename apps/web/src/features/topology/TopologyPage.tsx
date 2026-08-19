import { DeleteOutlined, DesktopOutlined, DownloadOutlined, FolderOpenOutlined, HddOutlined, LinkOutlined, PlusOutlined, SafetyCertificateOutlined, SaveOutlined, UploadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Background, BaseEdge, Controls, EdgeLabelRenderer, Handle, MiniMap, Position, ReactFlow, getStraightPath, useEdgesState, useNodesState, type Edge, type EdgeProps, type Node, type NodeProps } from "@xyflow/react";
import { Alert, Button, Card, Empty, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Tag, Typography, Upload, message } from "antd";
import { useCallback, useEffect, useState } from "react";
import { chooseDesktopExportPath, deleteTopology, exportTopology, getTopology, huaweiReadOnlyProbe, importTopology, listTopologies, saveTopology, saveTopologyExport, updateTopology, type SavedTopology, type TopologyLink, type TopologyNode } from "../../api/client";

type TopologyData = {
  label: string;
  kind: "switch" | "pc";
  ip?: string;
  prefix?: number;
  gateway?: string;
  sshHost?: string;
  sshPort?: number;
  sshUsername?: string;
  detectedModel?: string;
  detectedRelease?: string;
  protectedPortsText?: string;
};
type LinkData = { sourcePort?: string; targetPort?: string };
type TopologyEdge = Edge<LinkData, "interface">;
type ContextMenuTarget = { kind: "node" | "edge"; id: string; x: number; y: number };

function DeviceNode({ data, selected }: NodeProps<Node<TopologyData>>) {
  const isSwitch = data.kind === "switch";
  return (
    <div className={`topology-device ${isSwitch ? "switch-node" : "pc-node"} ${selected ? "device-selected" : ""}`}>
      <Handle className="topology-hidden-handle" type="target" position={Position.Left} id="hidden-target" style={{ left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} />
      <Handle className="topology-hidden-handle" type="source" position={Position.Right} id="hidden-source" style={{ left: "50%", top: "50%", transform: "translate(-50%, -50%)" }} />
      <div className="device-node-icon">{isSwitch ? <HddOutlined /> : <DesktopOutlined />}</div>
      <div className="device-node-meta">
        <strong>{data.label}</strong>
        <span>{isSwitch ? data.detectedModel || "待识别型号" : "终端节点"}</span>
      </div>
      <div className={`node-state ${isSwitch && data.sshHost ? "ready" : ""}`}>{isSwitch ? data.sshHost ? "SSH" : "交换机" : "PC"}</div>
    </div>
  );
}

function InterfaceEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps<TopologyEdge>) {
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  const sourceLabelX = sourceX + (targetX - sourceX) * 0.22;
  const sourceLabelY = sourceY + (targetY - sourceY) * 0.22;
  const targetLabelX = sourceX + (targetX - sourceX) * 0.78;
  const targetLabelY = sourceY + (targetY - sourceY) * 0.78;
  return (
    <>
      <BaseEdge id={id} path={edgePath} />
      <EdgeLabelRenderer>
        <div className="interface-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${sourceLabelX}px,${sourceLabelY}px)` }}>
          {data?.sourcePort || "未填写接口"}
        </div>
        <div className="interface-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${targetLabelX}px,${targetLabelY}px)` }}>
          {data?.targetPort || "未填写接口"}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const nodeTypes = { device: DeviceNode };
const edgeTypes = { interface: InterfaceEdge };

function errorDetail(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function TopologyPage({ onNavigatePlanning }: { onNavigatePlanning: () => void }) {
  const queryClient = useQueryClient();
  const savedTopologies = useQuery({ queryKey: ["topologies"], queryFn: listTopologies });
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<TopologyData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<TopologyEdge>([]);
  const [topologyId, setTopologyId] = useState<string>();
  const [topologyName, setTopologyName] = useState("未命名拓扑");
  const [selectedId, setSelectedId] = useState<string>();
  const [selectedEdgeId, setSelectedEdgeId] = useState<string>();
  const [connectionMode, setConnectionMode] = useState(false);
  const [linkSourceId, setLinkSourceId] = useState<string>();
  const [contextMenu, setContextMenu] = useState<ContextMenuTarget | null>(null);
  const [restoredLastTopology, setRestoredLastTopology] = useState(false);
  const [form] = Form.useForm<TopologyData>();
  const [linkForm] = Form.useForm<LinkData>();
  const [probeOpen, setProbeOpen] = useState(false);
  const [probeForm] = Form.useForm<{ host: string; port: number; username: string; password: string }>();
  const selected = nodes.find((node) => node.id === selectedId);
  const selectedEdge = edges.find((edge) => edge.id === selectedEdgeId);

  const clearNodeSelection = useCallback(() => {
    setSelectedId(undefined);
    form.resetFields();
    setNodes((current) => current.map((node) => ({ ...node, selected: false })));
  }, [form, setNodes]);

  const showDevice = useCallback((node: Node<TopologyData>) => {
    setSelectedId(node.id);
    setSelectedEdgeId(undefined);
    linkForm.resetFields();
    form.resetFields();
    form.setFieldsValue(node.data);
    setNodes((current) => current.map((item) => ({ ...item, selected: item.id === node.id })));
  }, [form, linkForm, setNodes]);

  const showLink = useCallback((edge: TopologyEdge) => {
    setSelectedEdgeId(edge.id);
    clearNodeSelection();
    linkForm.resetFields();
    linkForm.setFieldsValue({ sourcePort: edge.data?.sourcePort, targetPort: edge.data?.targetPort });
  }, [clearNodeSelection, linkForm]);

  const applySavedTopology = useCallback((topology: SavedTopology) => {
    setTopologyId(topology.id);
    setTopologyName(topology.name);
    setNodes(topology.graph.nodes.map((node) => ({
      id: node.id,
      type: "device",
      position: { x: node.x, y: node.y },
      data: {
        label: node.name,
        kind: node.kind === "switch" ? "switch" : "pc",
        ip: node.ip ?? undefined,
        prefix: node.prefix ?? undefined,
        gateway: node.gateway ?? undefined,
        sshHost: node.ssh_host ?? undefined,
        sshPort: node.ssh_port ?? undefined,
        sshUsername: node.ssh_username ?? undefined,
        detectedModel: node.detected_model ?? undefined,
        detectedRelease: node.detected_release ?? undefined,
        protectedPortsText: (node.protected_ports || []).join(", "),
      },
    })));
    setEdges(topology.graph.links.map((link) => ({
      id: link.id,
      type: "interface",
      source: link.source,
      target: link.target,
      sourceHandle: "hidden-source",
      targetHandle: "hidden-target",
      data: {
        sourcePort: link.source_port === "UNMAPPED" ? "" : link.source_port,
        targetPort: link.target_port === "UNMAPPED" ? "" : link.target_port,
      },
    })));
    setConnectionMode(false);
    setLinkSourceId(undefined);
    setSelectedEdgeId(undefined);
    form.resetFields();
    linkForm.resetFields();
    localStorage.setItem("last-topology-id", topology.id);
    localStorage.setItem("last-topology-revision", topology.revision_id);
  }, [form, linkForm, setEdges, setNodes]);

  const loadMutation = useMutation({
    mutationFn: getTopology,
    onSuccess: (topology) => {
      applySavedTopology(topology);
      message.success(`已打开“${topology.name}”`);
    },
    onError: (error) => message.error(errorDetail(error, "无法打开已保存拓扑。")),
  });

  useEffect(() => {
    if (restoredLastTopology || !savedTopologies.data) return;
    setRestoredLastTopology(true);
    const lastTopologyId = localStorage.getItem("last-topology-id");
    if (lastTopologyId && savedTopologies.data.some((item) => item.id === lastTopologyId)) {
      loadMutation.mutate(lastTopologyId);
    }
  }, [loadMutation, restoredLastTopology, savedTopologies.data]);

  const topologyPayload = () => ({
    name: topologyName.trim() || "未命名拓扑",
    nodes: nodes.map((node): TopologyNode => ({
      id: node.id,
      kind: node.data.kind,
      name: node.data.label,
      x: node.position.x,
      y: node.position.y,
      ip: node.data.ip || undefined,
      prefix: node.data.prefix ?? undefined,
      gateway: node.data.gateway || undefined,
      ssh_host: node.data.sshHost || undefined,
      ssh_port: node.data.sshPort ?? undefined,
      ssh_username: node.data.sshUsername || undefined,
      detected_model: node.data.detectedModel || undefined,
      detected_release: node.data.detectedRelease || undefined,
      protected_ports: (node.data.protectedPortsText || "").split(",").map((item) => item.trim()).filter(Boolean),
    })),
    links: edges.map((edge): TopologyLink => ({
      id: edge.id,
      source: edge.source,
      source_port: String(edge.data?.sourcePort || "UNMAPPED"),
      target: edge.target,
      target_port: String(edge.data?.targetPort || "UNMAPPED"),
    })),
  });

  const saveMutation = useMutation({
    mutationFn: () => topologyId ? updateTopology(topologyId, topologyPayload()) : saveTopology(topologyPayload()),
    onSuccess: (result) => {
      setTopologyId(result.id);
      setTopologyName(result.name);
      localStorage.setItem("last-topology-id", result.id);
      localStorage.setItem("last-topology-revision", result.revision_id);
      queryClient.invalidateQueries({ queryKey: ["topologies"] });
      message.success(`已保存“${result.name}”`);
    },
    onError: (error) => message.error(errorDetail(error, "拓扑保存失败。")),
  });

  const exportMutation = useMutation({
    mutationFn: async (id: string) => {
      const filename = `${topologyName || "topology"}.topology.json`;
      const destinationPath = await chooseDesktopExportPath(filename, "topology");
      if (destinationPath === null) return { kind: "cancelled" } as const;
      if (destinationPath) {
        return { kind: "saved", savedPath: (await saveTopologyExport(id, destinationPath)).saved_path } as const;
      }
      return { kind: "download", file: await exportTopology(id), filename } as const;
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
    onError: (error) => message.error(errorDetail(error, "拓扑导出失败。")),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteTopology,
    onSuccess: () => {
      newTopology();
      queryClient.invalidateQueries({ queryKey: ["topologies"] });
      message.success("已删除当前保存的拓扑。");
    },
    onError: (error) => message.error(errorDetail(error, "拓扑删除失败。")),
  });

  const importMutation = useMutation({
    mutationFn: ({ file, overwrite }: { file: File; overwrite?: boolean }) => importTopology(file, overwrite),
    onSuccess: (topology) => {
      applySavedTopology(topology);
      queryClient.invalidateQueries({ queryKey: ["topologies"] });
      message.success(`已导入并打开“${topology.name}”。`);
    },
    onError: (error, variables) => {
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status === 409 && !variables.overwrite) {
        Modal.confirm({
          title: "发现同名拓扑",
          content: "是否覆盖已有拓扑？覆盖会追加当前拓扑内容并保留已有配置任务引用。",
          okText: "覆盖导入",
          cancelText: "取消",
          onOk: () => importMutation.mutate({ file: variables.file, overwrite: true }),
        });
        return;
      }
      message.error(errorDetail(error, "拓扑导入失败。"));
    },
  });

  const probeMutation = useMutation({
    mutationFn: huaweiReadOnlyProbe,
    onSuccess: (result) => {
      if (!selected) return;
      setNodes((current) => current.map((node) => node.id === selected.id ? {
        ...node,
        data: { ...node.data, detectedModel: result.detected_model || undefined, detectedRelease: result.detected_release || undefined },
      } : node));
      form.setFieldsValue({ detectedModel: result.detected_model || undefined, detectedRelease: result.detected_release || undefined });
      setProbeOpen(false);
      message.success(`只读识别完成：${result.detected_model || "未识别型号"} / ${result.detected_release || "未识别版本"}`);
    },
    onError: (error) => message.error(errorDetail(error, "只读 SSH 探测失败；未发送任何配置命令。")),
  });

  const addNode = (kind: "switch" | "pc") => {
    const id = `${kind}-${Date.now()}`;
    const next: Node<TopologyData> = {
      id,
      type: "device",
      position: { x: 100 + nodes.length * 45, y: 120 + nodes.length * 35 },
      data: {
        label: kind === "switch" ? `SW${nodes.filter((node) => node.data.kind === "switch").length + 1}` : `PC${nodes.filter((node) => node.data.kind === "pc").length + 1}`,
        kind,
      },
    };
    setNodes((current) => [...current, next]);
    showDevice(next);
  };

  const updateSelected = (values: TopologyData) => {
    if (!selected) return;
    setNodes((current) => current.map((node) => node.id === selected.id ? { ...node, data: { ...node.data, ...values } } : node));
  };

  const startConnection = () => {
    clearNodeSelection();
    setSelectedEdgeId(undefined);
    linkForm.resetFields();
    setConnectionMode(true);
    setLinkSourceId(undefined);
    message.info("请先点击一台设备，再点击另一台设备完成连线。");
  };

  const handleNodeClick = (_: React.MouseEvent, node: Node<TopologyData>) => {
    setContextMenu(null);
    if (!connectionMode) {
      showDevice(node);
      return;
    }
    if (!linkSourceId) {
      setLinkSourceId(node.id);
      setNodes((current) => current.map((item) => ({ ...item, selected: item.id === node.id })));
      message.info(`已选“${node.data.label}”，请点击要连接的另一台设备。`);
      return;
    }
    if (linkSourceId === node.id) {
      message.warning("请点击另一台设备作为连线终点。");
      return;
    }
    if (edges.some((edge) => (edge.source === linkSourceId && edge.target === node.id) || (edge.source === node.id && edge.target === linkSourceId))) {
      message.warning("这两个设备之间已有链路；请选中该直线编辑接口名。");
      return;
    }
    const source = nodes.find((item) => item.id === linkSourceId);
    const nextEdge: TopologyEdge = {
      id: `link-${Date.now()}`,
      type: "interface",
      source: linkSourceId,
      target: node.id,
      sourceHandle: "hidden-source",
      targetHandle: "hidden-target",
      data: { sourcePort: "", targetPort: "" },
    };
    setEdges((current) => [...current, nextEdge]);
    setConnectionMode(false);
    setLinkSourceId(undefined);
    showLink(nextEdge);
    message.success(`已连接“${source?.data.label || "设备"}”与“${node.data.label}”，请在右侧填写接口名。`);
  };

  const saveLinkPorts = (values: LinkData) => {
    if (!selectedEdge) return;
    setEdges((current) => current.map((edge) => edge.id === selectedEdge.id ? {
      ...edge,
      data: { ...edge.data, sourcePort: values.sourcePort?.trim() || "", targetPort: values.targetPort?.trim() || "" },
    } : edge));
    message.success("链路接口名已保存到画布草稿。");
  };

  const removeNode = useCallback((nodeId: string) => {
    setNodes((current) => current.filter((node) => node.id !== nodeId));
    setEdges((current) => current.filter((edge) => edge.source !== nodeId && edge.target !== nodeId));
    if (selectedId === nodeId) clearNodeSelection();
    if (linkSourceId === nodeId) setLinkSourceId(undefined);
    setContextMenu(null);
  }, [clearNodeSelection, linkSourceId, selectedId, setEdges, setNodes]);

  const removeEdge = useCallback((edgeId: string) => {
    setEdges((current) => current.filter((edge) => edge.id !== edgeId));
    if (selectedEdgeId === edgeId) {
      setSelectedEdgeId(undefined);
      linkForm.resetFields();
    }
    setContextMenu(null);
  }, [linkForm, selectedEdgeId, setEdges]);

  const openNodeMenu = useCallback((event: React.MouseEvent, node: Node<TopologyData>) => {
    event.preventDefault();
    showDevice(node);
    setContextMenu({ kind: "node", id: node.id, x: event.clientX, y: event.clientY });
  }, [showDevice]);

  const openEdgeMenu = useCallback((event: React.MouseEvent, edge: TopologyEdge) => {
    event.preventDefault();
    showLink(edge);
    setContextMenu({ kind: "edge", id: edge.id, x: event.clientX, y: event.clientY });
  }, [showLink]);

  const newTopology = () => {
    setTopologyId(undefined);
    setTopologyName("未命名拓扑");
    setNodes([]);
    setEdges([]);
    setConnectionMode(false);
    setLinkSourceId(undefined);
    setSelectedEdgeId(undefined);
    form.resetFields();
    linkForm.resetFields();
    localStorage.removeItem("last-topology-id");
    localStorage.removeItem("last-topology-revision");
  };

  const linkEndpointName = (nodeId: string) => nodes.find((node) => node.id === nodeId)?.data.label || nodeId;

  return (
    <>
      <Typography.Title level={2} className="page-title">拓扑编辑</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">添加设备后点击“连线（绳）”，依次点选两台设备即可生成直线。选择直线后，在右侧填写两端的真实接口名。</Typography.Text>
      {contextMenu && <div className="topology-context-menu" style={{ left: contextMenu.x, top: contextMenu.y }}>
        <Button type="text" danger icon={<DeleteOutlined />} onClick={() => contextMenu.kind === "node" ? removeNode(contextMenu.id) : removeEdge(contextMenu.id)}>
          删除{contextMenu.kind === "node" ? "设备及关联链路" : "链路"}
        </Button>
      </div>}
      <div className="topology-layout">
        <Card className="panel-card palette-card" title="设备与拓扑">
          <Form layout="vertical" className="topology-name-form">
            <Form.Item label="拓扑名称">
              <Input value={topologyName} onChange={(event) => setTopologyName(event.target.value)} placeholder="例如：S5700 接入实验" />
            </Form.Item>
            <Form.Item label="已保存拓扑">
              <Select
                value={topologyId}
                placeholder="选择后打开"
                loading={savedTopologies.isLoading || loadMutation.isPending}
                options={(savedTopologies.data || []).map((item) => ({ value: item.id, label: item.name }))}
                onSelect={(id) => loadMutation.mutate(id)}
              />
            </Form.Item>
          </Form>
          <div className="node-palette">
            <Button icon={<HddOutlined />} onClick={() => addNode("switch")}>交换机</Button>
            <Button icon={<DesktopOutlined />} onClick={() => addNode("pc")}>PC 终端</Button>
            <Button type={connectionMode ? "primary" : "default"} icon={<LinkOutlined />} onClick={() => connectionMode ? (setConnectionMode(false), setLinkSourceId(undefined)) : startConnection()}>
              {connectionMode ? linkSourceId ? "取消连线" : "连线中：选择设备" : "连线（绳）"}
            </Button>
            <Button icon={<PlusOutlined />} onClick={newTopology}>新建拓扑</Button>
          </div>
          <div className="palette-note">节点没有预设端口和默认 IP。设备属性、链路接口名均只写入当前拓扑，点击保存后更新当前拓扑。</div>
        </Card>
        <div className={`topology-canvas ${connectionMode ? "connection-mode" : ""}`}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeClick={handleNodeClick}
            onEdgeClick={(_, edge) => showLink(edge)}
            onNodeContextMenu={openNodeMenu}
            onEdgeContextMenu={openEdgeMenu}
            onNodesDelete={(removed) => removed.forEach((node) => removeNode(node.id))}
            onEdgesDelete={(removed) => removed.forEach((edge) => removeEdge(edge.id))}
            onPaneClick={() => { setContextMenu(null); }}
            nodesConnectable={false}
            fitView
          >
            <Background gap={18} size={1} />
            <Controls />
            <MiniMap />
          </ReactFlow>
        </div>
        <Card className="panel-card property-card" title={selectedEdge ? "链路接口" : "设备属性"} extra={selectedEdge ? <Tag color="cyan">直线链路</Tag> : <Tag color={selected?.data.kind === "switch" ? "blue" : "green"}>{selected?.data.kind || "未选择"}</Tag>}>
          {selectedEdge ? <>
            <Typography.Paragraph type="secondary" className="link-endpoint-note">{linkEndpointName(selectedEdge.source)} ↔ {linkEndpointName(selectedEdge.target)}</Typography.Paragraph>
            <Form form={linkForm} layout="vertical" onFinish={saveLinkPorts}>
              <Form.Item name="sourcePort" label={`${linkEndpointName(selectedEdge.source)} 接口`}>
                <Input placeholder="例如：GE0/0/1" />
              </Form.Item>
              <Form.Item name="targetPort" label={`${linkEndpointName(selectedEdge.target)} 接口`}>
                <Input placeholder="例如：Ethernet0/0/1" />
              </Form.Item>
              <Button type="primary" htmlType="submit" icon={<SaveOutlined />}>保存接口名</Button>
            </Form>
          </> : !selected ? <Empty description="选择一个设备或一条链路" image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
            <Form form={form} layout="vertical" onValuesChange={(_, values) => updateSelected(values)}>
              <Form.Item name="label" label="名称"><Input /></Form.Item>
              <Form.Item name="ip" label="IP 地址（可选）"><Input placeholder="10.10.10.11" /></Form.Item>
              <Form.Item name="prefix" label="掩码前缀（可选）"><InputNumber min={0} max={128} style={{ width: "100%" }} /></Form.Item>
              <Form.Item name="gateway" label="网关（可选）"><Input placeholder="未填写时保持为空" /></Form.Item>
              {selected.data.kind === "switch" && <>
                <Form.Item name="sshHost" label="SSH IP（可选）"><Input placeholder="192.168.56.2" /></Form.Item>
                <Form.Item name="sshPort" label="SSH 端口（可选）"><InputNumber min={1} max={65535} placeholder="22" style={{ width: "100%" }} /></Form.Item>
                <Form.Item name="sshUsername" label="SSH 用户（可选）"><Input placeholder="<USERNAME>" /></Form.Item>
                <Form.Item name="detectedModel" label="只读识别型号"><Input disabled /></Form.Item>
                <Form.Item name="detectedRelease" label="只读识别版本"><Input disabled /></Form.Item>
                <Form.Item name="protectedPortsText" label="受保护端口（逗号分隔）"><Input placeholder="例如：GE0/0/2" /></Form.Item>
                <Button icon={<SafetyCertificateOutlined />} onClick={() => { probeForm.resetFields(); probeForm.setFieldsValue({ host: selected.data.sshHost, port: selected.data.sshPort || 22, username: selected.data.sshUsername }); setProbeOpen(true); }}>仅查询型号/版本</Button>
              </>}
            </Form>
          )}
        </Card>
      </div>
      <Space style={{ marginTop: 16 }}>
        <Button type="primary" icon={<SaveOutlined />} loading={saveMutation.isPending} onClick={() => saveMutation.mutate()}>保存拓扑</Button>
        <Button icon={<FolderOpenOutlined />} loading={loadMutation.isPending} disabled={!topologyId} onClick={() => topologyId && loadMutation.mutate(topologyId)}>重新加载当前拓扑</Button>
        <Button icon={<DownloadOutlined />} loading={exportMutation.isPending} disabled={!topologyId} onClick={() => topologyId && exportMutation.mutate(topologyId)}>导出当前拓扑</Button>
        <Upload accept=".json" showUploadList={false} beforeUpload={(file) => { importMutation.mutate({ file }); return false; }}>
          <Button icon={<UploadOutlined />} loading={importMutation.isPending}>导入拓扑</Button>
        </Upload>
        <Popconfirm title="删除当前保存的拓扑？" description="删除后该拓扑的全部修订会被移除；有配置任务引用时后端会拒绝删除。" okText="删除" cancelText="取消" onConfirm={() => topologyId && deleteMutation.mutate(topologyId)}>
          <Button danger icon={<DeleteOutlined />} loading={deleteMutation.isPending} disabled={!topologyId}>删除当前拓扑</Button>
        </Popconfirm>
        <Button icon={<PlusOutlined />} onClick={onNavigatePlanning}>前往配置规划</Button>
      </Space>
      <Modal title="华为交换机只读型号识别" open={probeOpen} onCancel={() => setProbeOpen(false)} onOk={() => probeForm.validateFields().then((values) => probeMutation.mutate({ ...values, command: "display version" }))} confirmLoading={probeMutation.isPending} okText="执行 display version">
        <Alert type="info" showIcon message="只读白名单" description="仅发送 display version；不会进入 system-view，不会查询或修改任何端口，也不会保存配置。" style={{ marginBottom: 16 }} />
        <Form form={probeForm} layout="vertical"><Form.Item name="host" label="SSH IP" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="port" label="端口" rules={[{ required: true }]}><InputNumber min={1} max={65535} style={{ width: "100%" }} /></Form.Item><Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="password" label="密码" rules={[{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item></Form>
      </Modal>
    </>
  );
}
