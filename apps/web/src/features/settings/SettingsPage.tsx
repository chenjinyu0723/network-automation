import { CheckCircleOutlined, SaveOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Form, Input, InputNumber, Row, Segmented, Typography, message } from "antd";
import { useEffect } from "react";
import { getProviderSettings, health, saveProviderSettings } from "../../api/client";

export function SettingsPage() {
  const [form] = Form.useForm();
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["provider-settings"], queryFn: getProviderSettings });
  const healthCheck = useQuery({ queryKey: ["health"], queryFn: health });
  const mutation = useMutation({
    mutationFn: saveProviderSettings,
    onSuccess: (data) => {
      queryClient.setQueryData(["provider-settings"], data);
      message.success("设置已保存；密钥仅写入本机系统凭据存储。")
      form.setFieldsValue({ llm_api_key: "", embedding_api_key: "" });
    },
    onError: () => message.error("保存设置失败。")
  });
  useEffect(() => { if (settings.data) form.setFieldsValue(settings.data); }, [form, settings.data]);

  return (
    <>
      <Typography.Title level={2} className="page-title">设置</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">LLM 与 Embedding 只通过 OpenAI 兼容接口调用；本项目固定使用 `httpx verify=False`。</Typography.Text>
      <Alert
        type="warning"
        showIcon
        message="TLS 证书校验已按确认要求固定关闭"
        description="仅应连接受信任的内网/私有模型端点；API Key 不保存到 SQLite 或前端状态。"
        style={{ marginBottom: 16 }}
      />
      <Card title="服务状态" style={{ marginBottom: 16 }}>
        <Typography.Text type={healthCheck.data?.status === "ok" ? "success" : "secondary"}>
          <CheckCircleOutlined /> 后端：{healthCheck.data?.status || "检查中"}
        </Typography.Text>
      </Card>
      <Form form={form} layout="vertical" onFinish={(values) => mutation.mutate(values)}>
        <Row gutter={16}>
          <Col span={12}><Card title="LLM">
            <Form.Item name="llm_base_url" label="Base URL"><Input placeholder="https://<HOST>/v1/" /></Form.Item>
            <Form.Item name="llm_api_key" label={`API Key ${settings.data?.llm_api_key_configured ? "（已配置；留空保持不变）" : ""}`}><Input.Password placeholder="<LLM_API_KEY>" /></Form.Item>
            <Form.Item name="llm_model" label="Model"><Input placeholder="<LLM_MODEL>" /></Form.Item>
            <Form.Item name="llm_temperature" label="Temperature"><InputNumber min={0} max={2} step={0.1} style={{ width: "100%" }} /></Form.Item>
            <Form.Item
              name="llm_thinking_mode"
              label="推理（thinking）策略"
              extra="自适应仅在需求理解、检索判断、命令计划、命令审查和结果诊断等推理型节点开启；静态校验、端口保护、执行和保存不会调用 LLM。若模型端点不支持 thinking 参数，系统会自动以关闭 thinking 的请求重试并在计划审计中标记。"
            >
              <Segmented
                block
                options={[
                  { label: "自适应（推荐）", value: "adaptive" },
                  { label: "始终开启", value: "always" },
                  { label: "始终关闭", value: "off" },
                ]}
              />
            </Form.Item>
          </Card></Col>
          <Col span={12}><Card title="Embedding">
            <Form.Item name="embedding_base_url" label="Base URL"><Input placeholder="https://<HOST>/v1/" /></Form.Item>
            <Form.Item name="embedding_api_key" label={`API Key ${settings.data?.embedding_api_key_configured ? "（已配置；留空保持不变）" : ""}`}><Input.Password placeholder="<EMBEDDING_API_KEY>" /></Form.Item>
            <Form.Item name="embedding_model" label="Model"><Input placeholder="<EMBEDDING_MODEL>" /></Form.Item>
            <Form.Item name="embedding_dimensions" label="向量维度"><InputNumber min={1} style={{ width: "100%" }} /></Form.Item>
          </Card></Col>
        </Row>
        <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={mutation.isPending} style={{ marginTop: 16 }}>保存本机设置</Button>
      </Form>
    </>
  );
}
