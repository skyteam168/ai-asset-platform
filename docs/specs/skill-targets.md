# corp-skill `--target` 适配表（architecture.md 10.4）

`--target` 可为：

1. **预设名**（不区分大小写）：映射到本机默认目录  
2. **任意路径**：原样 `expanduser` 后使用  

| 预设 | 目录 |
| --- | --- |
| `generic` / `aip` | `~/.aip/skills` |
| `cursor` | `~/.cursor/skills` |
| `claude` / `claude-desktop` | `~/.claude/skills` |

实现：`src/aip/cli/corp_skill.py` 的 `TARGET_PRESETS` / `resolve_target`。  
`login --target cursor` 会把预设名写入 `~/.aip/corp-skill.json`，后续 `install`/`list` 复用。
