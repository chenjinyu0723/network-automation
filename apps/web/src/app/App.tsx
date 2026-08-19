import {
  ApartmentOutlined,
  BookOutlined,
  ClusterOutlined,
  DeploymentUnitOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  SettingOutlined,
  WifiOutlined
} from "@ant-design/icons";
import { Badge, Layout, Menu, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { health } from "../api/client";
import { ManualsPage } from "../features/manuals/ManualsPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { TopologyPage } from "../features/topology/TopologyPage";
import { PlanningPage } from "../features/planning/PlanningPage";
import { ExecutionPage } from "../features/execution/ExecutionPage";
import { TemplatesPage } from "../features/templates/TemplatesPage";

const { Header, Sider, Content } = Layout;

const viewMeta = {
  topology: { title: "拓扑建模", icon: <ClusterOutlined /> },
  manuals: { title: "手册知识", icon: <BookOutlined /> },
  planning: { title: "配置规划", icon: <ApartmentOutlined /> },
  execution: { title: "逐台执行", icon: <SendOutlined /> },
  templates: { title: "配置模板", icon: <BookOutlined /> },
  settings: { title: "本机设置", icon: <SettingOutlined /> },
} as const;

export function App() {
  const [selected, setSelected] = useState<keyof typeof viewMeta>("topology");
  const service = useQuery({ queryKey: ["health"], queryFn: health, refetchInterval: 20_000 });
  const active = viewMeta[selected];
  return (
    <Layout className="app-shell">
      <Sider width={232} theme="dark" className="app-sider">
        <div className="brand">
          <div className="brand-mark"><DeploymentUnitOutlined /></div>
          <div>
            <strong>交换机自动配置</strong>
            <span>本地 AI 运行台</span>
          </div>
        </div>
        <div className="sider-caption">工作区</div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selected]}
          onClick={(event) => setSelected(event.key as keyof typeof viewMeta)}
          items={[
            { key: "topology", icon: <ClusterOutlined />, label: "拓扑编辑" },
            { key: "manuals", icon: <BookOutlined />, label: "手册管理" },
            { key: "templates", icon: <BookOutlined />, label: "模板管理" },
            { key: "planning", icon: <ApartmentOutlined />, label: "配置规划" },
            { key: "execution", icon: <SendOutlined />, label: "下发与结果" },
            { key: "settings", icon: <SettingOutlined />, label: "设置" }
          ]}
        />
        <div className="sider-footer">
          <Space size={8}><Badge status={service.data?.status === "ok" ? "success" : "default"} /><span>{service.data?.status === "ok" ? "本机服务就绪" : "正在连接本机服务"}</span></Space>
          <Tag icon={<SafetyCertificateOutlined />} color="cyan">人工确认后下发</Tag>
        </div>
      </Sider>
      <Layout>
        <Header className="app-header">
          <Space size={10} className="header-location"><span className="header-icon">{active.icon}</span><Typography.Text strong>{active.title}</Typography.Text></Space>
          <Space size={14} className="header-state"><WifiOutlined /><Typography.Text type="secondary">127.0.0.1 本地工作区</Typography.Text></Space>
        </Header>
        <Content className="app-content">
          <div hidden={selected !== "topology"}><TopologyPage onNavigatePlanning={() => setSelected("planning")} /></div>
          <div hidden={selected !== "manuals"}><ManualsPage /></div>
          <div hidden={selected !== "templates"}><TemplatesPage /></div>
          <div hidden={selected !== "planning"}><PlanningPage /></div>
          <div hidden={selected !== "execution"}><ExecutionPage /></div>
          <div hidden={selected !== "settings"}><SettingsPage /></div>
        </Content>
      </Layout>
    </Layout>
  );
}
