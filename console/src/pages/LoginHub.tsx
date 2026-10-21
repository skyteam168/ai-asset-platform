/** 全幅智造拓扑：旧系统插头 → 资产中台 → 生产业务 Agent（延伸至右侧登录区下方）。 */
import { t } from "../i18n";

const HUB = { cx: 360, cy: 200, r: 58 };

function plugs() {
  return [
    { id: "mes", label: "MES", x: 24, y: 48, delay: 0 },
    { id: "erp", label: "ERP", x: 24, y: 112, delay: 0.12 },
    { id: "db", label: t("数据库"), x: 24, y: 176, delay: 0.24 },
    { id: "api", label: "API", x: 24, y: 240, delay: 0.36 },
    { id: "kb", label: t("知识库"), x: 24, y: 304, delay: 0.48 },
  ];
}

function agents() {
  return [
    { id: "analytics", label: t("数据分析 Agent"), x: 560, y: 56, delay: 1.0 },
    { id: "material", label: t("物料安排 Agent"), x: 560, y: 128, delay: 1.12 },
    { id: "schedule", label: t("排产调度 Agent"), x: 560, y: 200, delay: 1.24 },
    { id: "quality", label: t("质检协同 Agent"), x: 560, y: 272, delay: 1.36 },
  ];
}

function PlugIcon({ x, y, label, delay }: { x: number; y: number; label: string; delay: number }) {
  return (
    <g className="login-hub-plug" style={{ ["--d" as string]: `${delay}s` }}>
      <rect x={x} y={y} width={76} height={42} rx={9} className="login-hub-chip" />
      <rect x={x + 76} y={y + 11} width={16} height={6} rx={1} className="login-hub-prong" />
      <rect x={x + 76} y={y + 25} width={16} height={6} rx={1} className="login-hub-prong" />
      <text x={x + 38} y={y + 26} textAnchor="middle" className="login-hub-label">
        {label}
      </text>
    </g>
  );
}

function AgentNode({ x, y, label, delay }: { x: number; y: number; label: string; delay: number }) {
  const w = 148;
  return (
    <g className="login-hub-agent" style={{ ["--d" as string]: `${delay}s` }}>
      <rect x={x} y={y} width={w} height={42} rx={11} className="login-hub-chip login-hub-chip-out" />
      <circle cx={x - 2} cy={y + 21} r={4.5} className="login-hub-socket-dot" />
      <text x={x + w / 2} y={y + 26} textAnchor="middle" className="login-hub-label login-hub-label-agent">
        {label}
      </text>
    </g>
  );
}

export function LoginHub() {
  const plugList = plugs();
  const agentList = agents();
  return (
    <div className="login-hub" aria-hidden>
      <svg viewBox="0 0 980 400" className="login-hub-svg" role="img" preserveAspectRatio="xMidYMid meet">
        <defs>
          <linearGradient id="hubGlow" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="rgba(255,255,255,0.28)" />
            <stop offset="100%" stopColor="rgba(255,255,255,0.06)" />
          </linearGradient>
          <linearGradient id="rail" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="rgba(255,255,255,0)" />
            <stop offset="40%" stopColor="rgba(255,255,255,0.14)" />
            <stop offset="100%" stopColor="rgba(160,255,220,0.08)" />
          </linearGradient>
          <filter id="softGlow" x="-40%" y="-40%" width="180%" height="180%">
            <feGaussianBlur stdDeviation="5" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* 延伸到右侧的能量轨 */}
        <rect x={700} y={40} width={260} height={320} rx={20} fill="url(#rail)" className="login-hub-rail" />

        {plugList.map((p, i) => {
          const sx = p.x + 92;
          const sy = p.y + 21;
          const ex = HUB.cx - HUB.r + 2;
          const ey = HUB.cy - 56 + i * 28;
          const mid = (sx + ex) / 2;
          const d = `M ${sx} ${sy} C ${mid} ${sy}, ${mid} ${ey}, ${ex} ${ey}`;
          return (
            <g key={p.id}>
              <path d={d} className="login-hub-wire login-hub-wire-in" style={{ ["--d" as string]: `${0.3 + i * 0.1}s` }} />
              <circle r="3.5" className="login-hub-packet login-hub-packet-in" style={{ ["--d" as string]: `${1.1 + i * 0.32}s` }}>
                <animateMotion dur="2.8s" begin={`${1.1 + i * 0.32}s`} repeatCount="indefinite" path={d} />
              </circle>
            </g>
          );
        })}

        {agentList.map((a, i) => {
          const sx = HUB.cx + HUB.r - 2;
          const sy = HUB.cy - 36 + i * 28;
          const ex = a.x - 2;
          const ey = a.y + 21;
          const mid = (sx + ex) / 2;
          const d = `M ${sx} ${sy} C ${mid} ${sy}, ${mid} ${ey}, ${ex} ${ey}`;
          // 继续向右延伸到登录卡片下方
          const d2 = `M ${a.x + 148} ${ey} C ${a.x + 220} ${ey}, ${820} ${ey}, ${940} ${ey}`;
          return (
            <g key={a.id}>
              <path d={d} className="login-hub-wire login-hub-wire-out" style={{ ["--d" as string]: `${0.85 + i * 0.1}s` }} />
              <path d={d2} className="login-hub-wire login-hub-wire-extend" style={{ ["--d" as string]: `${1.15 + i * 0.1}s` }} />
              <circle r="3.5" className="login-hub-packet login-hub-packet-out" style={{ ["--d" as string]: `${1.9 + i * 0.35}s` }}>
                <animateMotion dur="2.5s" begin={`${1.9 + i * 0.35}s`} repeatCount="indefinite" path={d} />
              </circle>
              <circle r="2.5" className="login-hub-packet login-hub-packet-out" style={{ ["--d" as string]: `${2.6 + i * 0.35}s` }}>
                <animateMotion dur="3.2s" begin={`${2.6 + i * 0.35}s`} repeatCount="indefinite" path={d2} />
              </circle>
            </g>
          );
        })}

        <g className="login-hub-core" filter="url(#softGlow)" style={{ ["--hub-x" as string]: `${HUB.cx}px`, ["--hub-y" as string]: `${HUB.cy}px` }}>
          <circle cx={HUB.cx} cy={HUB.cy} r={HUB.r + 22} className="login-hub-ring" />
          <circle cx={HUB.cx} cy={HUB.cy} r={HUB.r + 10} className="login-hub-ring-inner" />
          <circle cx={HUB.cx} cy={HUB.cy} r={HUB.r} fill="url(#hubGlow)" className="login-hub-disk" />
          <g className="login-hub-ports">
            <rect x={HUB.cx - 24} y={HUB.cy - 16} width={11} height={32} rx={3} />
            <rect x={HUB.cx - 5} y={HUB.cy - 16} width={11} height={32} rx={3} />
            <rect x={HUB.cx + 14} y={HUB.cy - 16} width={11} height={32} rx={3} />
          </g>
          <text x={HUB.cx} y={HUB.cy + 48} textAnchor="middle" className="login-hub-hub-caption">
            {t("资产中台")}
          </text>
        </g>

        {plugList.map((p) => (
          <PlugIcon key={p.id} x={p.x} y={p.y} label={p.label} delay={p.delay} />
        ))}
        {agentList.map((a) => (
          <AgentNode key={a.id} x={a.x} y={a.y} label={a.label} delay={a.delay} />
        ))}

        <text x={100} y={22} className="login-hub-flow-tag">
          {t("插入 · 旧系统")}
        </text>
        <text x={630} y={22} className="login-hub-flow-tag">
          {t("分发 · 生产 Agent")}
        </text>
        <text x={860} y={22} textAnchor="middle" className="login-hub-flow-tag">
          {t("智造任务面")}
        </text>
      </svg>
    </div>
  );
}
