// web/src/features/pipelines/AccountSelector.tsx
// 内容分发节点的账号选择器：平台动态规则 + 账号级启用开关（对齐 demo.pen / image.png 设计）。
//
// config.account_selection = { platforms, extra_account_ids, excluded_account_ids }
//   - platforms：选中平台 → 该平台「全部已启用账号」默认纳入（运行时解析，含未来新增）。
//   - excluded_account_ids：规则内被单独停用的账号。
//   - extra_account_ids：规则外单独追加（兼容旧扁平 account_ids；当前 UI 主要由平台规则驱动）。
// 启用指示 = 右侧方块（蓝框 + 蓝色实心方块），非传统对勾；点击整张卡片切换启用。
import type { Account } from "../../types";

type Selection = { platforms: string[]; extra: number[]; excluded: number[] };

function readSelection(config: Record<string, unknown>): Selection {
  const sel = config.account_selection as
    | { platforms?: string[]; extra_account_ids?: number[]; excluded_account_ids?: number[] }
    | undefined;
  if (sel && typeof sel === "object") {
    return {
      platforms: [...(sel.platforms ?? [])],
      extra: [...(sel.extra_account_ids ?? [])],
      excluded: [...(sel.excluded_account_ids ?? [])],
    };
  }
  const legacy = (config.account_ids as number[] | undefined) ?? []; // 兼容旧节点
  return { platforms: [], extra: [...legacy], excluded: [] };
}

function patchFor(s: Selection): Record<string, unknown> {
  return {
    account_selection: {
      platforms: s.platforms,
      extra_account_ids: s.extra,
      excluded_account_ids: s.excluded,
    },
    account_ids: undefined, // 清掉旧扁平字段，统一走 account_selection
  };
}

export function AccountSelector({ accounts, config, onChange }: {
  accounts: Account[];
  config: Record<string, unknown>;
  onChange: (patch: Record<string, unknown>) => void;
}) {
  // 仅可分发账号（distribution_enabled）进选择器；停用账号运行时也会被解析器过滤，不展示避免误解。
  const usable = accounts.filter((a) => a.distribution_enabled);
  const sel = readSelection(config);
  const commit = (next: Selection) => onChange(patchFor(next));

  const platformName = new Map<string, string>();
  for (const a of usable) if (!platformName.has(a.platform_code)) platformName.set(a.platform_code, a.platform_name);
  const allCodes = [...platformName.keys()].sort();

  const allActive =
    allCodes.length > 0 &&
    sel.platforms.length === allCodes.length &&
    allCodes.every((c) => sel.platforms.includes(c));

  const isOn = (a: Account) =>
    !sel.excluded.includes(a.id) && (sel.platforms.includes(a.platform_code) || sel.extra.includes(a.id));

  // 展示分组的平台：全部 → 所有平台；否则 = 规则平台 ∪ 追加账号所在平台。
  const extraCodes = usable.filter((a) => sel.extra.includes(a.id)).map((a) => a.platform_code);
  const shownCodes = allActive
    ? allCodes
    : [...new Set([...sel.platforms, ...extraCodes])].sort();

  const setAccount = (a: Account, on: boolean) => {
    const platforms = [...sel.platforms];
    let extra = sel.extra.filter((id) => id !== a.id);
    let excluded = sel.excluded.filter((id) => id !== a.id);
    if (on) {
      if (!platforms.includes(a.platform_code)) extra = [...extra, a.id]; // 规则外 → 追加
    } else if (platforms.includes(a.platform_code)) {
      excluded = [...excluded, a.id]; // 规则内 → 排除
    }
    commit({ platforms, extra, excluded });
  };

  const toggleGroup = (code: string) => {
    const group = usable.filter((a) => a.platform_code === code);
    const target = !group.every(isOn); // 当前未全开 → 全开；已全开 → 全关
    const inRule = sel.platforms.includes(code);
    let extra = [...sel.extra];
    let excluded = [...sel.excluded];
    for (const a of group) {
      extra = extra.filter((id) => id !== a.id);
      excluded = excluded.filter((id) => id !== a.id);
      if (target && !inRule) extra = [...extra, a.id];
      if (!target && inRule) excluded = [...excluded, a.id];
    }
    commit({ platforms: [...sel.platforms], extra, excluded });
  };

  const clickAll = () => commit({ ...sel, platforms: [...allCodes] });
  const clickPlatform = (code: string) => {
    let platforms: string[];
    if (allActive) platforms = [code];
    else if (sel.platforms.includes(code)) {
      platforms = sel.platforms.filter((c) => c !== code);
      if (platforms.length === 0) platforms = [...allCodes]; // 取消到空 → 回退「全部」
    } else platforms = [...sel.platforms, code];
    commit({ ...sel, platforms });
  };

  if (usable.length === 0) {
    return <div className="schemeEmpty">暂无可分发账号，请先到「账号」添加并启用分发</div>;
  }

  return (
    <>
      <div className="agentFieldLabel">分发账号</div>
      <div className="distPlatformRow">
        <span className="distPlatformLabel">平台</span>
        <button type="button" className={`distPill${allActive ? " on" : ""}`} onClick={clickAll}>全部</button>
        {allCodes.map((c) => (
          <button key={c} type="button"
            className={`distPill${!allActive && sel.platforms.includes(c) ? " on" : ""}`}
            onClick={() => clickPlatform(c)}>
            {platformName.get(c)}
          </button>
        ))}
      </div>
      {shownCodes.length === 0 && <div className="schemeEmpty">请选择上方分发平台</div>}
      {shownCodes.map((code) => {
        const group = usable.filter((a) => a.platform_code === code);
        const allOn = group.length > 0 && group.every(isOn);
        return (
          <div className="distGroup" key={code}>
            <button type="button" className="distSelectAll" onClick={() => toggleGroup(code)}
              title={`全选 / 取消 ${platformName.get(code)}`}>
              <span className="distSelectAllText">全选</span>
              <span className={`distToggle${allOn ? " on" : ""}`}><span className="distToggleBox" /></span>
            </button>
            <div className="distCards">
              {group.map((a) => {
                const on = isOn(a);
                const initial = (a.display_name || "?").trim().charAt(0) || "?";
                return (
                  <button type="button" key={a.id} className={`distCard${on ? "" : " off"}`}
                    onClick={() => setAccount(a, !on)}>
                    <span className="distAvatar">
                      <span className="distAvatarText">{initial}</span>
                      {a.status === "valid" && <span className="distDot" />}
                    </span>
                    <span className="distName">{a.display_name}</span>
                    {a.contact && <span className="distPhone">{a.contact}</span>}
                    <span className="distSpacer" />
                    <span className="distPlat">{a.platform_name}</span>
                    <span className={`distToggle${on ? " on" : ""}`}><span className="distToggleBox" /></span>
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </>
  );
}
