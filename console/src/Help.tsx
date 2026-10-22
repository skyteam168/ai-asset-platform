/** 顶栏「帮助」打开的使用说明：按控制台实际功能编写，侧栏分节 + 正文步骤。 */
import {
  ApartmentOutlined,
  ApiOutlined,
  AuditOutlined,
  BookOutlined,
  CheckSquareOutlined,
  CloudUploadOutlined,
  ExperimentOutlined,
  LinkOutlined,
  QuestionCircleOutlined,
  RocketOutlined,
  SafetyCertificateOutlined,
  AlertOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { Button, Drawer, Layout, Menu, Typography } from "antd";
import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { t } from "./i18n";

const { Title, Paragraph, Text } = Typography;

type SectionId =
  | "quick"
  | "approvals"
  | "assets"
  | "catalog"
  | "distribution"
  | "grants"
  | "access"
  | "risk"
  | "audit"
  | "llm"
  | "orgs"
  | "roles"
  | "faq";

interface HelpSection {
  id: SectionId;
  label: string;
  icon: ReactNode;
  subtitle: string;
  blocks: { heading: string; steps: string[] }[];
}

function sections(): HelpSection[] {
  return [
    {
      id: "quick",
      label: t("快速开始"),
      icon: <RocketOutlined />,
      subtitle: t("登录后按角色看到菜单；用搜索跳转，用铃铛处理待办。"),
      blocks: [
        {
          heading: t("第一次使用"),
          steps: [
            t("用平台账号登录：用户名、口令，以及认证器中的 6 位动态码（TOTP）。菜单与数据范围由角色决定，不是外部系统同步来的。"),
            t("认识界面：左侧为功能菜单；顶部有功能搜索（Ctrl / ⌘ + K）、服务状态、帮助、明暗主题、语言与账号菜单。页签条可保留多个已打开页面。"),
            t("右上角铃铛显示待办数量：门禁/使用申请审批、调用确认、工作流人工任务。点击进入「审批待办」。"),
            t("通知通道：待办可经邮件（日志/SMTP）与通用 IM Webhook 投递；由运维配置 AIP_NOTIFY_IM_WEBHOOK_URL，无需在控制台单独管理。"),
            t("若看不到某个菜单，说明当前账号没有对应角色；请联系平台管理员在「组织与用户」中分配。"),
          ],
        },
        {
          heading: t("推荐上手路径"),
          steps: [
            t("平台管理员：组织与用户 → 系统连接 → 资产与门禁 → 授权上限 → 消费方。"),
            t("资产开发者：登记连接与 Connector → 新建资产版本 → 提交门禁与运行测试 → 等待 G5/G6 审批。"),
            t("数据 Owner / 业务 Owner：处理审批待办中的安全审核、业务验收与写操作确认。"),
            t("分公司管理员：消费方与绑定、使用申请、本公司范围内的审计与风控查看。"),
          ],
        },
      ],
    },
    {
      id: "approvals",
      label: t("审批待办"),
      icon: <CheckSquareOutlined />,
      subtitle: t("三类待办：门禁与使用申请、调用确认、工作流任务。"),
      blocks: [
        {
          heading: t("门禁与使用申请"),
          steps: [
            t("G5 业务验收、G6 安全审核：资产版本进入 in_review 后出现；按角色与范围认领，填写意见后通过或驳回。"),
            t("使用申请：分公司在授权上限内为消费方申请某资产与环境；本公司管理员审批，申请人不能审批自己的单。"),
            t("熔断恢复（风险）：自动 Suspended 后须写明修复情况，由另一位数据 Owner 复核。"),
          ],
        },
        {
          heading: t("调用确认"),
          steps: [
            t("写操作或策略要求确认时，执行前冻结参数（界面只显示摘要）。通过后平台按冻结内容执行一次。"),
            t("「审批」类不能由被代表的用户本人确认；「本人确认」只能由该用户在控制台处理。"),
          ],
        },
        {
          heading: t("工作流任务"),
          steps: [
            t("Workflow 中的确认、审批或补录节点会生成任务；可点 Run 链接查看步骤与当前节点。"),
            t("补录任务按 input_schema 提交 JSON；驳回需写明理由。指定处理人的任务也可由有效代理人处理。"),
          ],
        },
      ],
    },
    {
      id: "assets",
      label: t("资产与门禁"),
      icon: <ApiOutlined />,
      subtitle: t("登记 Tool / Skill / Workflow 等资产，走 G0–G8 门禁后发布。"),
      blocks: [
        {
          heading: t("资产与版本"),
          steps: [
            t("新建资产时指定类型、归属组织与密级。点击行打开抽屉，可创建版本并编辑 Spec。"),
            t("版本一旦发布不可改写。状态大致为：draft → testing → in_review → approved → canary → active。"),
            t("Skill 类型需在 draft 上传已签名的 zip 包后再提交门禁。"),
          ],
        },
        {
          heading: t("门禁流程"),
          steps: [
            t("准备：为版本添加功能用例与权限用例，填写验收单模板；套件可配置工具选择评测。"),
            t("提交（G0）：静态检查；Tool 可能给出 PII 字段建议。通过后进入 testing。"),
            t("运行测试（G1–G4）：Test Lab；失败会退回 draft。通过后进入 in_review 并打开 G5/G6。"),
            t("灰度（G7）与全量（G8）：指标达标后由平台管理员全量，版本变为 active。"),
          ],
        },
      ],
    },
    {
      id: "catalog",
      label: t("资产目录"),
      icon: <BookOutlined />,
      subtitle: t("查看已发布能力与套件，便于选型与对接。"),
      blocks: [
        {
          heading: t("使用方式"),
          steps: [
            t("「能力目录」列出最新发布快照中的 Tool：能力 ID、风险级别、密级、外发类别与所属套件。"),
            t("「套件」展示 bundle 组合。搜索支持能力名、套件名与描述。"),
            t("目录只读；要改内容请到「资产与门禁」维护版本并重新走门禁。"),
          ],
        },
      ],
    },
    {
      id: "distribution",
      label: t("分发与 Skill"),
      icon: <CloudUploadOutlined />,
      subtitle: t("Skill 目录、Webhook 订阅与套件相关分发。"),
      blocks: [
        {
          heading: t("Skill"),
          steps: [
            t("浏览可安装的 Skill 版本；撤回状态会标明。安装前检查会提示消费方缺少的 Tool 授权。"),
            t("开发侧在资产抽屉上传包并完成门禁后，才会出现在目录中。"),
          ],
        },
        {
          heading: t("Webhook"),
          steps: [
            t("为消费方登记回调 URL 与事件类型；创建时返回一次性明文密钥，之后只能轮换。"),
            t("可查看投递记录、启用或停用端点。密钥与凭据不会出现在审计明文中。"),
          ],
        },
      ],
    },
    {
      id: "grants",
      label: t("授权"),
      icon: <SafetyCertificateOutlined />,
      subtitle: t("集团上限 → 分公司使用申请 → 下放授权 → 定期复核。"),
      blocks: [
        {
          heading: t("授权上限"),
          steps: [
            t("平台管理员为分公司下发可使用的资产集合与密级上限；分公司只能在上限内申请。"),
          ],
        },
        {
          heading: t("授权与使用申请"),
          steps: [
            t("「授权」查看已下放给消费方等主体的授权及过期时间。"),
            t("「使用申请」由分公司发起；审批通过后生成授权。"),
            t("「访问复核」可发起活动：到期未确认的授权在关闭活动时会被撤销。"),
          ],
        },
      ],
    },
    {
      id: "access",
      label: t("接入管理"),
      icon: <LinkOutlined />,
      subtitle: t("消费方凭据与到旧系统的出站连接。"),
      blocks: [
        {
          heading: t("消费方与绑定"),
          steps: [
            t("注册消费方（client_id），签发凭据；可登记用户断言验签公钥与用户绑定。"),
            t("凭据吊销立即生效。配额（每日调用/写次数）可在消费方上配置。"),
          ],
        },
        {
          heading: t("系统连接"),
          steps: [
            t("登记 base_url、环境与 TLS 模式。生产环境的 http / insecure 属于传输例外，需数据 Owner 审批（生产常需两人）。"),
            t("「连接测试」做真实握手（不带业务凭据）；通过后可生成 pinned / custom_ca 信任草稿。"),
            t("同一页可管理 Connector 实例（声明式、mock、上游 MCP 等），非 mock 须绑定系统连接。"),
          ],
        },
      ],
    },
    {
      id: "risk",
      label: t("风控与计量"),
      icon: <AlertOutlined />,
      subtitle: t("规则命中事件、熔断恢复与调用计量。"),
      blocks: [
        {
          heading: t("风险事件与规则"),
          steps: [
            t("查看待处理事件；告警类可「关闭」，熔断类须走恢复复核，不能直接销案。"),
            t("平台管理员可调整规则参数、启用状态，并手动触发扫描。"),
            t("「计量」按日汇总调用、失败与写操作，用于容量与异常观察。"),
          ],
        },
      ],
    },
    {
      id: "audit",
      label: t("审计"),
      icon: <AuditOutlined />,
      subtitle: t("检索决策与操作记录，校验哈希链与锚点。"),
      blocks: [
        {
          heading: t("使用方式"),
          steps: [
            t("按事件类型、trace_id、时间范围查询；分公司管理员默认只看本公司。"),
            t("「校验哈希链与锚点」核对完整性；日志中不会出现明文凭据或未脱敏业务数据。"),
          ],
        },
      ],
    },
    {
      id: "llm",
      label: t("LLM 配置"),
      icon: <ExperimentOutlined />,
      subtitle: t("仅平台管理员：公司模型白名单与日 token 预算。"),
      blocks: [
        {
          heading: t("配置要点"),
          steps: [
            t("为公司配置允许的模型名列表；未在白名单内的调用会被网关拒绝。"),
            t("设置每日 token 预算并查看当日用量，防止失控消耗。"),
          ],
        },
      ],
    },
    {
      id: "orgs",
      label: t("组织与用户"),
      icon: <ApartmentOutlined />,
      subtitle: t("自建组织树、用户、角色与密级；不信任外部部门声明。"),
      blocks: [
        {
          heading: t("日常操作"),
          steps: [
            t("左侧组织树选择范围；右侧管理用户：新建、批量导入、分配控制面角色与业务角色。"),
            t("密级申请与审批在此流程内完成。组织节点可移动或停用（须符合约束）。"),
          ],
        },
      ],
    },
    {
      id: "roles",
      label: t("角色说明"),
      icon: <TeamOutlined />,
      subtitle: t("菜单可见性按角色裁剪；每个接口仍做服务端鉴权。"),
      blocks: [
        {
          heading: t("控制面角色"),
          steps: [
            t("平台管理员：全局配置、上限、连接策略、LLM、全量发布等。"),
            t("资产开发者：资产版本、连接登记、门禁测试与 Skill 包。"),
            t("数据 Owner：传输例外、安全审核（G6）、写确认、熔断恢复。"),
            t("业务 Owner：业务验收（G5）等业务侧审批。"),
            t("分公司管理员：本公司消费方、使用申请、部分审计与风控。"),
            t("审计员：风控与审计只读（及组织查看，视配置而定）。"),
          ],
        },
      ],
    },
    {
      id: "faq",
      label: t("常见问题"),
      icon: <QuestionCircleOutlined />,
      subtitle: t("登录、权限与发布相关的常见疑问。"),
      blocks: [
        {
          heading: t("排障"),
          steps: [
            t("动态码错误：确认认证器时间同步，使用当前 30 秒窗口内的码。"),
            t("接口 403：角色或组织范围不足；菜单隐藏不代表可绕过，后端会再次检查。"),
            t("连接一直「待审批」：生产传输例外需要足够数量的数据 Owner 审批。"),
            t("版本卡在 testing / in_review：检查用例与验收单是否齐全，并在审批待办处理 G5/G6。"),
            t("顶栏「服务异常」：控制面 /healthz 不可达，请检查本地或集群服务是否已启动。"),
          ],
        },
      ],
    },
  ];
}

export function HelpDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const items = useMemo(() => sections(), []);
  const [active, setActive] = useState<SectionId>("quick");
  const current = items.find((s) => s.id === active) ?? items[0];

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={920}
      title={t("使用说明")}
      destroyOnClose={false}
      styles={{ body: { padding: 0, height: "100%" } }}
      className="aip-help-drawer"
    >
      <Layout className="aip-help-layout">
        <Layout.Sider width={220} className="aip-help-sider" theme="light">
          <Menu
            mode="inline"
            selectedKeys={[active]}
            items={items.map((s) => ({ key: s.id, icon: s.icon, label: s.label }))}
            onClick={(e) => setActive(e.key as SectionId)}
          />
        </Layout.Sider>
        <Layout.Content className="aip-help-body">
          <div className="aip-help-hero">
            <Title level={3} style={{ margin: 0 }}>
              {current.label}
            </Title>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              {current.subtitle}
            </Paragraph>
          </div>
          {current.blocks.map((block) => (
            <section key={block.heading} className="aip-help-card">
              <Title level={5}>{block.heading}</Title>
              <ol className="aip-help-steps">
                {block.steps.map((step) => (
                  <li key={step}>
                    <Text>{step}</Text>
                  </li>
                ))}
              </ol>
            </section>
          ))}
          <div className="aip-help-foot">
            <Text type="secondary">{t("快捷键：Ctrl / ⌘ + K 搜索功能。界面语言与明暗主题在右上角切换，选择会被记住。")}</Text>
            <Button type="link" onClick={onClose} style={{ paddingInline: 0 }}>
              {t("关闭说明")}
            </Button>
          </div>
        </Layout.Content>
      </Layout>
    </Drawer>
  );
}
