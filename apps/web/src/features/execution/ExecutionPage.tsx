import { SendOutlined } from "@ant-design/icons";
import { useMutation } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Form, Input, InputNumber, List, Space, Tag, Typography, message } from "antd";
import { useState } from "react";
import { executeHuaweiPlan, executePcPing, type ExecutionRun, type PcPingRun } from "../../api/client";

export function ExecutionPage() {
  const [form] = Form.useForm<{ taskId: string; planId: string; host: string; port: number; username: string; password: string }>();
  const [result, setResult] = useState<ExecutionRun>();
  const [pingForm] = Form.useForm<{ host: string; port: number; username: string; password: string; os_family: "linux" | "windows"; target_ip: string }>();
  const [ping, setPing] = useState<PcPingRun>();
  const execute = useMutation({
    mutationFn: (values: { taskId: string; planId: string; host: string; port: number; username: string; password: string }) => executeHuaweiPlan(values.taskId, values.planId, { host: values.host, port: values.port, username: values.username, password: values.password }),
    onSuccess: (run) => {
      setResult(run);
      message.info(run.status === "completed" ? "设备已验证并保存。" : "本次执行已停止，请查看安全记录。");
    },
    onError: () => message.error("执行请求失败；不会自动重试。")
  });
  const pingMutation = useMutation({
    mutationFn: (values: { host: string; port: number; username: string; password: string; os_family: "linux" | "windows"; target_ip: string }) => result ? executePcPing(result.id, values) : Promise.reject(new Error("设备执行不存在")),
    onSuccess: (run) => { setPing(run); message.info(run.success ? "PC ping 验收通过。" : "PC ping 未通过，请检查输出。"); },
    onError: () => message.error("PC ping 验收无法执行；设备必须先完成验证与 save。")
  });
  return <>
    <Typography.Title level={2} className="page-title">下发与结果</Typography.Title>
    <Typography.Text type="secondary" className="page-subtitle">每次只下发一台已审批设备。密码仅用于本次 SSH 连接，不保存到浏览器、SQLite 或日志。</Typography.Text>
    <Alert type="warning" showIcon style={{ margin: "16px 0" }} message="硬性门禁" description="型号、手册版本、审批 revision、冻结的 SSH 地址/端口、端口保护和前一台设备状态均须通过。验证失败绝不执行 save。" />
    <Card title="单台确认下发" style={{ maxWidth: 800 }}>
      <Form form={form} layout="vertical" initialValues={{ port: 22 }} onFinish={(values) => execute.mutate({ taskId: values.taskId, planId: values.planId, host: values.host, port: values.port, username: values.username, password: values.password })}>
        <Form.Item name="taskId" label="配置任务 ID" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="planId" label="设备计划 ID" rules={[{ required: true }]}><Input /></Form.Item>
        <Space style={{ width: "100%" }} size="middle"><Form.Item name="host" label="SSH IP" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="port" label="端口" rules={[{ required: true }]}><InputNumber min={1} max={65535} /></Form.Item><Form.Item name="username" label="用户" rules={[{ required: true }]}><Input /></Form.Item></Space>
        <Form.Item name="password" label="本次 SSH 密码" rules={[{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item>
        <Button type="primary" danger htmlType="submit" icon={<SendOutlined />} loading={execute.isPending}>确认仅下发本设备</Button>
      </Form>
    </Card>
    {result && <Card title={`执行记录 ${result.id.slice(0, 8)}`} style={{ marginTop: 16 }}>
      <Descriptions size="small" column={2}><Descriptions.Item label="状态"><Tag color={result.status === "completed" ? "success" : "error"}>{result.status}</Tag></Descriptions.Item><Descriptions.Item label="错误">{result.error_message || "-"}</Descriptions.Item><Descriptions.Item label="预检">{(result.preflight.errors || []).join("；") || "通过"}</Descriptions.Item><Descriptions.Item label="save">{JSON.stringify(result.save)}</Descriptions.Item></Descriptions>
      <List size="small" dataSource={result.commands} renderItem={(entry) => <List.Item><Space direction="vertical" size={0}><Typography.Text code>{entry.phase} · {entry.command}</Typography.Text><Typography.Text type={entry.success ? "secondary" : "danger"}>{entry.output || (entry.success ? "已发送" : "失败")}</Typography.Text></Space></List.Item>} />
    </Card>}
    {result?.status === "completed" && <Card title="PC SSH ping 验收（可选）" style={{ marginTop: 16 }}>
      <Alert type="info" showIcon message="仅允许 ping 到明确填写的 IPv4 地址；PC 密码不保存。" style={{ marginBottom: 12 }} />
      <Form form={pingForm} layout="inline" initialValues={{ port: 22, os_family: "linux" }} onFinish={(values) => pingMutation.mutate(values)}><Form.Item name="host" rules={[{ required: true }]}><Input placeholder="PC SSH IP" /></Form.Item><Form.Item name="port"><InputNumber min={1} max={65535} /></Form.Item><Form.Item name="username" rules={[{ required: true }]}><Input placeholder="PC 用户" /></Form.Item><Form.Item name="password" rules={[{ required: true }]}><Input.Password placeholder="PC 密码" /></Form.Item><Form.Item name="target_ip" rules={[{ required: true }]}><Input placeholder="Ping 目标 IPv4" /></Form.Item><Button htmlType="submit" loading={pingMutation.isPending}>执行 ping</Button></Form>
      {ping && <Typography.Paragraph style={{ marginTop: 12 }}><Typography.Text type={ping.success ? "success" : "danger"}>{ping.success ? "通过" : ping.error_message || "未通过"}</Typography.Text><pre>{ping.output || ping.command}</pre></Typography.Paragraph>}
    </Card>}
  </>;
}
