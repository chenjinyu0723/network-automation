import {
  ApartmentOutlined,
  BookOutlined,
  ClusterOutlined,
  DatabaseOutlined,
  DeploymentUnitOutlined,
  SendOutlined,
  SettingOutlined
} from "@ant-design/icons";
import { Layout, Menu, Typography } from "antd";
import { useState } from "react";
import { ManualsPage } from "../features/manuals/ManualsPage";
import { ModelsPage } from "../features/models/ModelsPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { TopologyPage } from "../features/topology/TopologyPage";
import { PlanningPage } from "../features/planning/PlanningPage";
import { ExecutionPage } from "../features/execution/ExecutionPage";

const { Header, Sider, Content } = Layout;

const views = {
  topology: <TopologyPage />,
  manuals: <ManualsPage />,
  models: <ModelsPage />,
  planning: <PlanningPage />,
  execution: <ExecutionPage />,
  settings: <SettingsPage />
} as const;

export function App() {
  const [selected, setSelected] = useState<keyof typeof views>("topology");

  return (
    <Layout className="app-shell">
      <Sider width={232} theme="dark" className="app-sider">
        <div className="brand">
          <DeploymentUnitOutlined />
          <div>
            <strong>交换机自动配置</strong>
            <span>Local Agent Workspace</span>
          </div>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selected]}
          onClick={(event) => setSelected(event.key as keyof typeof views)}
          items={[
            { key: "topology", icon: <ClusterOutlined />, label: "拓扑编辑" },
            { key: "manuals", icon: <BookOutlined />, label: "手册管理" },
            { key: "models", icon: <DatabaseOutlined />, label: "型号库" },
            { key: "planning", icon: <ApartmentOutlined />, label: "配置规划" },
            { key: "execution", icon: <SendOutlined />, label: "下发与结果" },
            { key: "settings", icon: <SettingOutlined />, label: "设置" }
          ]}
        />
      </Sider>
      <Layout>
        <Header className="app-header">
          <Typography.Text>本地单用户 · 逐台审批 · 手册证据驱动</Typography.Text>
          <Typography.Text type="secondary">运行数据保存在项目 data/ 目录</Typography.Text>
        </Header>
        <Content className="app-content">{views[selected]}</Content>
      </Layout>
    </Layout>
  );
}
