import { DeleteOutlined, DownloadOutlined, EditOutlined, InboxOutlined, ReloadOutlined, SearchOutlined, UploadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Descriptions, Input, Modal, Popconfirm, Progress, Select, Space, Table, Tag, Typography, Upload, message } from "antd";
import type { UploadProps } from "antd";
import { useEffect, useState } from "react";
import {
  activeManualSearch,
  chooseDesktopExportPath,
  createEmbeddingIndex,
  deleteManual,
  exportManual,
  getImportJob,
  importManual,
  listImportJobs,
  listManuals,
  retryImportJob,
  saveManualExport,
  type ActiveManualSearch,
  type ImportJob,
  updateManual,
  uploadManual,
  type Manual
} from "../../api/client";

const statusColor: Record<string, string> = {
  queued: "default",
  running: "processing",
  completed: "success",
  completed_with_issues: "warning",
  failed: "error"
};

const cliProfileLabel: Record<Manual["cli_profile"], string> = {
  auto: "自动识别",
  huawei_vrp: "Huawei VRP",
  h3c_comware: "H3C Comware",
  cisco_ios: "Cisco IOS",
  arista_eos: "Arista EOS",
  generic_manual: "通用手册"
};

const isTerminal = (status: string) => ["completed", "completed_with_issues", "failed", "cancelled"].includes(status);

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function ManualsPage() {
  const queryClient = useQueryClient();
  const [job, setJob] = useState<ImportJob | null>(null);
  const [searchManual, setSearchManual] = useState<{ id: string; name: string } | null>(null);
  const [searchText, setSearchText] = useState("");
  const [searchResult, setSearchResult] = useState<ActiveManualSearch | null>(null);
  const [uploadBrand, setUploadBrand] = useState("");
  const [uploadRelease, setUploadRelease] = useState("");
  const [editingManual, setEditingManual] = useState<Manual | null>(null);
  const [editFilename, setEditFilename] = useState("");
  const [editBrand, setEditBrand] = useState("");
  const [editRelease, setEditRelease] = useState("");
  const [editCliProfile, setEditCliProfile] = useState<Manual["cli_profile"]>("auto");
  const importJobs = useQuery({
    queryKey: ["manual-imports"],
    queryFn: listImportJobs,
    refetchInterval: (query) => query.state.data?.some((item) => !isTerminal(item.status)) ? 2500 : false
  });
  const hasActiveJob = Boolean(job && !isTerminal(job.status));
  const manuals = useQuery({ queryKey: ["manuals"], queryFn: listManuals, refetchInterval: hasActiveJob ? 4000 : false });
  const mutation = useMutation({
    mutationFn: (file: File) => uploadManual(file, uploadBrand.trim() || undefined, uploadRelease.trim() || undefined),
    onSuccess: (newJob) => {
      setJob(newJob);
      queryClient.invalidateQueries({ queryKey: ["manuals"] });
      queryClient.invalidateQueries({ queryKey: ["manual-imports"] });
      message.success("手册已入队；正在本地抽取知识。");
    },
    onError: () => message.error("上传失败，请检查文件与后端服务。")
  });
  const retry = useMutation({
    mutationFn: retryImportJob,
    onSuccess: (newJob) => {
      setJob(newJob);
      queryClient.invalidateQueries({ queryKey: ["manual-imports"] });
      message.success("导入任务已重新启动；将从已提交页面继续。");
    },
    onError: () => message.error("重试未启动，请刷新后确认任务状态。")
  });
  const buildEmbedding = useMutation({
    mutationFn: createEmbeddingIndex,
    onSuccess: () => message.success("Embedding 索引已在本地后台启动；仍可继续使用 FTS5。"),
    onError: () => message.error("无法建立索引：请先在设置页配置 Embedding 接口。")
  });
  const activeSearch = useMutation({
    mutationFn: () => activeManualSearch(searchManual!.id, searchText),
    onSuccess: (result) => setSearchResult(result),
    onError: () => message.error("主动检索失败；请确认 LLM、Embedding 与手册索引状态。")
  });
  const saveManualEdit = useMutation({
    mutationFn: () => editingManual ? updateManual(editingManual.id, {
      original_filename: editFilename,
      brand: editBrand.trim() || null,
      release: editRelease.trim() || null,
      cli_profile: editCliProfile,
    }) : Promise.reject(new Error("手册不存在")),
    onSuccess: () => {
      setEditingManual(null);
      queryClient.invalidateQueries({ queryKey: ["manuals"] });
      message.success("手册信息已保存。");
    },
    onError: () => message.error("手册信息保存失败；请检查名称是否为空。")
  });
  const exportMutation = useMutation({
    mutationFn: async (id: string) => {
      const filename = `manual-${id.slice(0, 8)}.manual.zip`;
      const destinationPath = await chooseDesktopExportPath(filename, "manual");
      if (destinationPath === null) return { kind: "cancelled" } as const;
      if (destinationPath) {
        return { kind: "saved", savedPath: (await saveManualExport(id, destinationPath)).saved_path } as const;
      }
      return { kind: "download", file: await exportManual(id), filename } as const;
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
    onError: () => message.error("手册导出失败。"),
  });
  const deleteMutation = useMutation({
    mutationFn: deleteManual,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["manuals"] });
      message.success("手册及其抽取内容已删除。");
    },
    onError: () => message.error("手册删除失败；若有配置任务引用，请先处理相关任务。"),
  });
  const importArchive = useMutation({
    mutationFn: ({ file, overwrite }: { file: File; overwrite?: boolean }) => importManual(file, overwrite),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["manuals"] });
      message.success("手册归档已导入。原文、抽取结果、型号映射、命令和向量一并恢复。");
    },
    onError: (error, variables) => {
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status === 409 && !variables.overwrite) {
        Modal.confirm({
          title: "发现同名手册",
          content: "是否覆盖已有手册？覆盖会替换原文、抽取结果、型号映射、命令库和 Embedding。",
          okText: "覆盖导入",
          cancelText: "取消",
          onOk: () => importArchive.mutate({ file: variables.file, overwrite: true }),
        });
        return;
      }
      message.error("手册归档导入失败。" );
    },
  });

  useEffect(() => {
    if (!job || isTerminal(job.status)) return;
    const timer = window.setInterval(async () => {
      const fresh = await getImportJob(job.id);
      setJob(fresh);
      if (isTerminal(fresh.status)) {
        queryClient.invalidateQueries({ queryKey: ["manuals"] });
        queryClient.invalidateQueries({ queryKey: ["manual-imports"] });
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [job, queryClient]);

  useEffect(() => {
    if (!job && importJobs.data?.[0]) setJob(importJobs.data[0]);
  }, [importJobs.data, job]);

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
        注入不同品牌、版本和格式的手册。完成抽取后，在配置规划页直接选择对应手册作为命令上下文。
      </Typography.Text>
      <div className="toolbar">
        <div style={{ width: "min(630px, 100%)" }}>
          <Space.Compact style={{ width: "100%", marginBottom: 8 }}>
            <Input value={uploadBrand} onChange={(event) => setUploadBrand(event.target.value)} placeholder="品牌（可不填）" />
            <Input value={uploadRelease} onChange={(event) => setUploadRelease(event.target.value)} placeholder="版本（可不填）" />
          </Space.Compact>
          <Upload.Dragger {...props} disabled={mutation.isPending}>
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">导入 CHM / PDF / HTML / 文本手册</p>
            <p className="ant-upload-hint">品牌和版本默认留空；CHM 由本机 7-Zip 解包，不会上传到外部服务。</p>
          </Upload.Dragger>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} onClick={() => manuals.refetch()}>刷新列表</Button>
          <Upload accept=".manual.zip" showUploadList={false} beforeUpload={(file) => { importArchive.mutate({ file }); return false; }}>
            <Button icon={<UploadOutlined />} loading={importArchive.isPending}>导入手册归档</Button>
          </Upload>
        </Space>
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
        scroll={{ x: 1240 }}
        columns={[
          { title: "手册", dataIndex: "original_filename", ellipsis: true },
          { title: "品牌", dataIndex: "brand", render: (value) => value || "待识别" },
          { title: "版本", dataIndex: "release", render: (value) => value || "待识别" },
          {
            title: "CLI 方言",
            dataIndex: "cli_profile",
            render: (value: Manual["cli_profile"]) => cliProfileLabel[value] || value
          },
          { title: "格式", dataIndex: "file_format", render: (value) => <Tag>{value}</Tag> },
          { title: "状态", dataIndex: "status", render: (value) => <Tag color={statusColor[value]}>{value}</Tag> },
          {
            title: "导入进度",
            render: (_, row) => {
              const latest = importJobs.data?.find((item) => item.manual_id === row.id);
              return latest ? `${latest.stage} ${latest.progress_current}/${latest.progress_total || "?"}` : "-";
            }
          },
          { title: "页面", dataIndex: "page_count" },
          { title: "命令", dataIndex: "command_count" },
          { title: "失败页", dataIndex: "issue_count" },
          {
            title: "操作",
            width: 390,
            render: (_, row) => <div className="manual-action-group">
              <Button size="small" icon={<EditOutlined />} onClick={() => {
                setEditingManual(row);
                setEditFilename(row.original_filename);
                setEditBrand(row.brand || "");
                setEditRelease(row.release || "");
                setEditCliProfile(row.cli_profile || "auto");
              }}>编辑</Button>
              {row.status.startsWith("completed") && <>
                <Button size="small" loading={buildEmbedding.isPending} onClick={() => buildEmbedding.mutate(row.id)}>构建 Embedding</Button>
                <Button size="small" icon={<SearchOutlined />} onClick={() => { setSearchManual({ id: row.id, name: row.original_filename }); setSearchResult(null); }}>主动检索</Button>
              </>}
              <Button size="small" icon={<DownloadOutlined />} loading={exportMutation.isPending} onClick={() => exportMutation.mutate(row.id)}>导出</Button>
              <Popconfirm title="删除这本手册？" description="将删除原文、解析页面、命令、型号映射和向量；有配置任务引用时会拒绝。" okText="删除" cancelText="取消" onConfirm={() => deleteMutation.mutate(row.id)}>
                <Button size="small" danger icon={<DeleteOutlined />} loading={deleteMutation.isPending}>删除</Button>
              </Popconfirm>
            </div>
          }
        ]}
      />
      <Space direction="vertical" size={0} style={{ marginTop: 12 }}>
        <Typography.Text type="secondary">使用说明：抽取完成的手册可用于规划；检索证据、拓扑信息和校验结果会作为命令草案旁的提示，最终是否下发由你逐台判断。</Typography.Text>
      </Space>
      <Modal
        open={Boolean(searchManual)}
        title={searchManual ? `主动检索：${searchManual.name}` : "主动检索"}
        width={980}
        onCancel={() => { setSearchManual(null); setSearchResult(null); }}
        footer={<Space><Button onClick={() => { setSearchManual(null); setSearchResult(null); }}>关闭</Button><Button type="primary" icon={<SearchOutlined />} loading={activeSearch.isPending} disabled={searchText.trim().length < 3} onClick={() => activeSearch.mutate()}>检索</Button></Space>}
      >
        <Input.TextArea value={searchText} onChange={(event) => setSearchText(event.target.value)} autoSize={{ minRows: 3, maxRows: 5 }} placeholder="输入要完成的网络功能或约束" />
        {searchResult && <>
          <Descriptions size="small" column={3} style={{ marginTop: 16 }}>
            <Descriptions.Item label="结论"><Tag color={searchResult.status === "found" ? "success" : "warning"}>{searchResult.status}</Tag></Descriptions.Item>
            <Descriptions.Item label="轮次">{searchResult.rounds.length}</Descriptions.Item>
            <Descriptions.Item label="选中命令">{searchResult.selected_command_ids.length}</Descriptions.Item>
          </Descriptions>
          <Table
            size="small"
            rowKey={(row) => `${row.document_id}-${row.command_id || "page"}`}
            dataSource={searchResult.candidates}
            pagination={{ pageSize: 5, size: "small" }}
            style={{ marginTop: 12 }}
            columns={[
              { title: "候选", render: (_, row) => row.canonical_name || row.title },
              { title: "命令语法", render: (_, row) => row.syntax.join(" ; ") || "-", ellipsis: true },
              { title: "来源", dataIndex: "source_path", ellipsis: true },
              { title: "检索", render: (_, row) => row.retrieval_sources.join(" + ") },
              { title: "分数", dataIndex: "score", width: 76 }
            ]}
            expandable={{ expandedRowRender: (row) => <Typography.Paragraph style={{ marginBottom: 0 }}>{row.excerpt}</Typography.Paragraph> }}
          />
          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
            {searchResult.rounds.map((round) => `第 ${round.round} 轮：${round.queries.join(" / ")}（${round.candidate_count} 条，${round.llm.status}）`).join("；")}
          </Typography.Paragraph>
        </>}
      </Modal>
      <Modal
        open={Boolean(editingManual)}
        title="编辑手册信息"
        okText="保存"
        cancelText="取消"
        confirmLoading={saveManualEdit.isPending}
        onCancel={() => setEditingManual(null)}
        onOk={() => saveManualEdit.mutate()}
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Input value={editFilename} onChange={(event) => setEditFilename(event.target.value)} placeholder="显示在手册列表中的名称" />
          <Input value={editBrand} onChange={(event) => setEditBrand(event.target.value)} placeholder="品牌（留空表示无）" />
          <Input value={editRelease} onChange={(event) => setEditRelease(event.target.value)} placeholder="版本（留空表示无）" />
          <Select
            value={editCliProfile}
            onChange={setEditCliProfile}
            options={[
              { value: "auto", label: "自动识别（按品牌；未知品牌不注入会话命令）" },
              { value: "huawei_vrp", label: "Huawei VRP" },
              { value: "h3c_comware", label: "H3C Comware" },
              { value: "cisco_ios", label: "Cisco IOS" },
              { value: "arista_eos", label: "Arista EOS" },
              { value: "generic_manual", label: "通用手册 CLI" },
            ]}
          />
        </Space>
      </Modal>
    </>
  );
}
