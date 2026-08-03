import { InboxOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Descriptions, Progress, Space, Table, Tag, Typography, Upload, message } from "antd";
import type { UploadProps } from "antd";
import { useEffect, useState } from "react";
import { createEmbeddingIndex, getImportJob, listManuals, retryImportJob, type ImportJob, uploadManual } from "../../api/client";

const statusColor: Record<string, string> = {
  queued: "default",
  running: "processing",
  completed: "success",
  completed_with_issues: "warning",
  failed: "error"
};

export function ManualsPage() {
  const queryClient = useQueryClient();
  const [job, setJob] = useState<ImportJob | null>(null);
  const manuals = useQuery({ queryKey: ["manuals"], queryFn: listManuals, refetchInterval: job ? 4000 : false });
  const mutation = useMutation({
    mutationFn: (file: File) => uploadManual(file, "Huawei"),
    onSuccess: (newJob) => {
      setJob(newJob);
      queryClient.invalidateQueries({ queryKey: ["manuals"] });
      message.success("手册已入队；正在本地抽取知识。");
    },
    onError: () => message.error("上传失败，请检查文件与后端服务。")
  });
  const retry = useMutation({
    mutationFn: retryImportJob,
    onSuccess: (newJob) => {
      setJob(newJob);
      message.success("导入任务已重新启动；将从已提交页面继续。");
    },
    onError: () => message.error("重试未启动，请刷新后确认任务状态。")
  });
  const buildEmbedding = useMutation({
    mutationFn: createEmbeddingIndex,
    onSuccess: () => message.success("Embedding 索引已在本地后台启动；仍可继续使用 FTS5。"),
    onError: () => message.error("无法建立索引：请先在设置页配置 Embedding 接口。")
  });

  useEffect(() => {
    if (!job || ["completed", "completed_with_issues", "failed", "cancelled"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      const fresh = await getImportJob(job.id);
      setJob(fresh);
      if (["completed", "completed_with_issues", "failed", "cancelled"].includes(fresh.status)) {
        queryClient.invalidateQueries({ queryKey: ["manuals"] });
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [job, queryClient]);

  const props: UploadProps = {
    accept: ".chm,.html,.htm,.txt,.md,.pdf",
    multiple: false,
    showUploadList: false,
    beforeUpload: (file) => {
      mutation.mutate(file);
      return false;
    }
  };

  return (
    <>
      <Typography.Title level={2} className="page-title">手册管理</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">
        注入不同品牌、版本和格式的手册。型号候选先进入审核状态，未发布型号不会出现在拓扑选择器。
      </Typography.Text>
      <div className="toolbar">
        <Upload.Dragger {...props} disabled={mutation.isPending} style={{ maxWidth: 630 }}>
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">导入 CHM / PDF / HTML / 文本手册</p>
          <p className="ant-upload-hint">CHM 将由本机 7-Zip 解包；不会上传到外部服务。</p>
        </Upload.Dragger>
        <Button icon={<ReloadOutlined />} onClick={() => manuals.refetch()}>刷新列表</Button>
      </div>
      {job && (
        <Card size="small" style={{ marginBottom: 16 }} title="当前导入任务">
          <Descriptions size="small" column={3}>
            <Descriptions.Item label="阶段">{job.stage}</Descriptions.Item>
            <Descriptions.Item label="状态"><Tag color={statusColor[job.status]}>{job.status}</Tag></Descriptions.Item>
            <Descriptions.Item label="详情">{job.detail || "-"}</Descriptions.Item>
          </Descriptions>
          <Progress
            percent={job.progress_total ? Math.round((job.progress_current / job.progress_total) * 100) : 0}
            status={job.status === "failed" ? "exception" : job.status.startsWith("completed") ? "success" : "active"}
            format={() => `${job.progress_current}/${job.progress_total || "?"}`}
          />
          {job.status === "failed" && <Button icon={<ReloadOutlined />} loading={retry.isPending} onClick={() => retry.mutate(job.id)}>从断点重试</Button>}
        </Card>
      )}
      <Table
        rowKey="id"
        loading={manuals.isLoading}
        dataSource={manuals.data || []}
        pagination={{ pageSize: 10 }}
        columns={[
          { title: "手册", dataIndex: "original_filename", ellipsis: true },
          { title: "品牌", dataIndex: "brand", render: (value) => value || "待识别" },
          { title: "版本", dataIndex: "release", render: (value) => value || "待识别" },
          { title: "格式", dataIndex: "file_format", render: (value) => <Tag>{value}</Tag> },
          { title: "状态", dataIndex: "status", render: (value) => <Tag color={statusColor[value]}>{value}</Tag> },
          { title: "页面", dataIndex: "page_count" },
          { title: "命令", dataIndex: "command_count" },
          { title: "型号候选", dataIndex: "model_count" },
          { title: "失败页", dataIndex: "issue_count" },
          { title: "检索增强", render: (_, row) => row.status.startsWith("completed") && <Button size="small" loading={buildEmbedding.isPending} onClick={() => buildEmbedding.mutate(row.id)}>构建 Embedding</Button> }
        ]}
      />
      <Space direction="vertical" size={0} style={{ marginTop: 12 }}>
        <Typography.Text type="secondary">质量门：导入完成不代表可直接下发；命令适用范围与型号映射仍需在“型号库”审核。</Typography.Text>
      </Space>
    </>
  );
}
