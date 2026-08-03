import { CheckOutlined, SearchOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Input, Modal, Select, Space, Table, Tag, Typography, message } from "antd";
import { useMemo, useState } from "react";
import { listModels, updateModel, type DeviceModel } from "../../api/client";

const statusColor: Record<string, string> = { published: "success", candidate: "warning", rejected: "error" };

export function ModelsPage() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<DeviceModel>();
  const [parentId, setParentId] = useState<string>();
  const [alias, setAlias] = useState("");
  const models = useQuery({ queryKey: ["models"], queryFn: () => listModels(false) });
  const publish = useMutation({
    mutationFn: (id: string) => updateModel(id, { review_status: "published" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
      message.success("型号已发布，可在拓扑编辑器中选择。");
    },
    onError: () => message.error("发布失败。")
  });
  const correction = useMutation({
    mutationFn: () => {
      if (!editing) return Promise.reject(new Error("未选择型号"));
      return updateModel(editing.id, { parent_id: parentId, aliases_to_add: alias ? [alias] : [] });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
      setEditing(undefined);
      setAlias("");
      message.success("型号映射已保存；发布状态保持独立审核。");
    },
    onError: () => message.error("型号映射保存失败。")
  });
  const filtered = useMemo(
    () => (models.data || []).filter((item) => item.canonical_name.toLowerCase().includes(query.toLowerCase())),
    [models.data, query]
  );

  return (
    <>
      <Typography.Title level={2} className="page-title">型号库</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">
        型号由手册证据自动候选；系列自动发布，产品族和 SKU 默认需要人工审核后才可用于设备。
      </Typography.Text>
      <Space className="toolbar">
        <Input prefix={<SearchOutlined />} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索 S5735 / S5755 / S6700…" style={{ width: 320 }} />
      </Space>
      <Table
        rowKey="id"
        loading={models.isLoading}
        dataSource={filtered}
        pagination={{ pageSize: 20 }}
        columns={[
          { title: "品牌", dataIndex: "brand", width: 100 },
          { title: "型号 / 系列", dataIndex: "canonical_name" },
          { title: "层级", dataIndex: "level", render: (value) => <Tag>{value}</Tag> },
          { title: "父级", dataIndex: "parent_id", render: (value) => (models.data || []).find((item) => item.id === value)?.canonical_name || "-" },
          { title: "证据", dataIndex: "evidence_count" },
          { title: "置信度", dataIndex: "confidence", render: (value) => `${value}%` },
          { title: "状态", dataIndex: "review_status", render: (value) => <Tag color={statusColor[value]}>{value}</Tag> },
          {
            title: "操作",
            render: (_, row) => <Space size="small">
              <Button size="small" onClick={() => { setEditing(row); setParentId(row.parent_id || undefined); setAlias(""); }}>修正映射</Button>
              {row.review_status !== "published" && <Button size="small" icon={<CheckOutlined />} loading={publish.isPending} onClick={() => publish.mutate(row.id)}>发布</Button>}
            </Space>
          }
        ]}
      />
      <Modal title="修正型号层级与别名" open={Boolean(editing)} onCancel={() => setEditing(undefined)} onOk={() => correction.mutate()} confirmLoading={correction.isPending} okText="保存映射">
        <Typography.Paragraph>当前型号：<Typography.Text code>{editing?.canonical_name}</Typography.Text>。将其父级指向经过人工确认的产品族或系列；该操作不自动发布型号。</Typography.Paragraph>
        <Select value={parentId} onChange={setParentId} allowClear showSearch style={{ width: "100%" }} placeholder="选择父级型号" options={(models.data || []).filter((item) => item.id !== editing?.id && item.level !== "sku").map((item) => ({ value: item.id, label: `${item.level} · ${item.canonical_name}` }))} />
        <Input value={alias} onChange={(event) => setAlias(event.target.value)} placeholder="可选：添加设备回显别名，例如 S5700-28C-HI" style={{ marginTop: 12 }} />
      </Modal>
    </>
  );
}
