import { DeleteOutlined, DownloadOutlined, EditOutlined, EyeOutlined, UploadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Collapse, Descriptions, Empty, Input, List, Modal, Popconfirm, Space, Table, Tag, Typography, Upload, message } from "antd";
import { useState } from "react";
import { chooseDesktopExportPath, deleteTemplate, exportTemplate, getTemplate, importTemplate, listTemplates, saveTemplateExport, updateTemplate, type ConfigurationTemplateDetail, type ConfigurationTemplateSummary } from "../../api/client";

const formatTime = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });

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
      <Typography.Text type="secondary" className="page-subtitle">这里保存经你审阅的配置结果快照。新任务可选择模板让 LLM 参考实施方法，但当前拓扑与需求始终优先，不会直接复用旧设备或命令参数。</Typography.Text>
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
  const nodeName = new Map(detail.topology.nodes.map((node) => [node.id, node.name]));
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Descriptions bordered size="small" column={2}>
        <Descriptions.Item label="来源手册">{detail.manual_name || "未记录"}</Descriptions.Item>
        <Descriptions.Item label="保存时间">{formatTime(detail.created_at)}</Descriptions.Item>
        <Descriptions.Item label="简介" span={2}>{detail.description || "-"}</Descriptions.Item>
      </Descriptions>
      <Card size="small" title="拓扑快照">
        <Typography.Text strong>设备与终端</Typography.Text>
        <List size="small" dataSource={detail.topology.nodes} renderItem={(node) => <List.Item><Space><Tag color={node.kind === "switch" ? "cyan" : "green"}>{node.kind === "switch" ? "交换机" : "PC"}</Tag><Typography.Text>{node.name}</Typography.Text>{node.model_id && <Typography.Text type="secondary">型号：{node.model_id}</Typography.Text>}{node.ip && <Typography.Text type="secondary">IP：{node.ip}</Typography.Text>}</Space></List.Item>} />
        <Typography.Text strong>连线</Typography.Text>
        <List size="small" dataSource={detail.topology.links} locale={{ emptyText: "未保存连线" }} renderItem={(link) => <List.Item>{nodeName.get(link.source) || link.source} <Typography.Text code>{link.source_port}</Typography.Text> <span>连接</span> {nodeName.get(link.target) || link.target} <Typography.Text code>{link.target_port}</Typography.Text></List.Item>} />
      </Card>
      <Card size="small" title="配置要求"><Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{detail.requirement_text || "-"}</Typography.Paragraph></Card>
      <Card size="small" title="配置思路"><Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{detail.planning_idea || "-"}</Typography.Paragraph></Card>
      <Card size="small" title="分设备配置命令">
        <Collapse items={detail.device_plans.map((plan) => ({
          key: plan.device_node_id,
          label: plan.display_name,
          children: <><Typography.Paragraph type="secondary">意图：{JSON.stringify(plan.intent)}</Typography.Paragraph><Typography.Paragraph className="command-preview">{plan.commands.join("\n") || "未生成命令"}</Typography.Paragraph></>
        }))} />
      </Card>
    </Space>
  );
}
