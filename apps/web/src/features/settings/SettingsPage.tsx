import { ApiOutlined, CheckCircleOutlined, SaveOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Col, Form, Input, InputNumber, Row, Segmented, Space, Typography, message } from "antd";
import { useEffect } from "react";
import {
  getProviderSettings,
  health,
  saveProviderSettings,
  testEmbeddingProvider,
  testLlmProvider,
} from "../../api/client";

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
  const testLlm = useMutation({
    mutationFn: testLlmProvider,
    onSuccess: (result) => message.success(`LLM 连通：${result.model}${result.thinking_fallback ? "；thinking 已降级" : ""}`),
    onError: () => message.error("LLM 连通性检查失败；请检查本机设置与端点。")
  });
  const testEmbedding = useMutation({
    mutationFn: testEmbeddingProvider,
    onSuccess: (result) => {
      const mismatch = result.requested_dimensions && result.requested_dimensions !== result.dimensions;
      message[mismatch ? "warning" : "success"](
        `Embedding 连通：${result.model}，实际向量维度 ${result.dimensions}` +
        (mismatch ? `；当前设置为 ${result.requested_dimensions}` : "")
      );
    },
    onError: (error: unknown) => {
      const detail = (error as { response?: { data?: { detail?: string } } }).response?.data?.detail;
      message.error(detail || "Embedding 连通性检查失败；请先保存设置并检查端点、模型、API Key 与向量维度。")
    }
  });
  useEffect(() => { if (settings.data) form.setFieldsValue(settings.data); }, [form, settings.data]);

  return (
    <>
      <Typography.Title level={2} className="page-title">设置</Typography.Title>
      <Typography.Text type="secondary" className="page-subtitle">LLM 与 Embedding 只通过 OpenAI 兼容接口调用；本项目固定使用 `httpx verify=False`。</Typography.Text>
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
                extra="自适应仅在需求理解、命令计划/修订、命令审查和结果诊断等推理型节点开启；检索判断、静态校验、端口保护、执行和保存不启用 thinking。若模型端点不支持 thinking 参数，系统会按兼容模式自动重试并在计划审计中标记。"
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
            <Form.Item name="embedding_base_url" label="Base URL" extra="可填写 /v1/ 或完整的 /v1/embeddings 地址，系统会自动规范化。"><Input placeholder="http://<HOST>:<PORT>/v1/embeddings" /></Form.Item>
            <Form.Item name="embedding_api_key" label={`API Key ${settings.data?.embedding_api_key_configured ? "（已配置；留空保持不变）" : ""}`}><Input.Password placeholder="<EMBEDDING_API_KEY>" /></Form.Item>
            <Form.Item name="embedding_model" label="Model"><Input placeholder="<EMBEDDING_MODEL>" /></Form.Item>
            <Form.Item name="embedding_dimensions" label="向量维度" extra="例如 Qwen3-Embedding-4B 填 2560；留空则使用服务默认维度。"><InputNumber min={1} style={{ width: "100%" }} /></Form.Item>
            <Form.Item
              name="embedding_batch_size"
              label="每批请求条数"
              extra="默认 2；范围 1-20。数值越小越兼容限流较严的端点，但构建索引耗时更长；运行中的任务保持启动时的设置。"
            ><InputNumber min={1} max={20} precision={0} style={{ width: "100%" }} /></Form.Item>
          </Card></Col>
        </Row>
        <Space style={{ marginTop: 16 }} wrap>
          <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={mutation.isPending}>保存本机设置</Button>
          <Button icon={<ApiOutlined />} loading={testLlm.isPending} onClick={() => testLlm.mutate()}>测试 LLM 连接</Button>
          <Button icon={<ApiOutlined />} loading={testEmbedding.isPending} onClick={() => testEmbedding.mutate()}>测试 Embedding 连接</Button>
        </Space>
      </Form>
    </>
  );
}
