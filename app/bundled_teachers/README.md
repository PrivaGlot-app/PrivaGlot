# 预置教师档案（随代码分发）

v2 多厂商（#15）：**教师目录随 TTS 厂商走**——按厂商分子目录，教师 id
带命名空间（`mimo:dean` / `zhipu:jam`），跨厂商不可见他厂角色；切换
语音厂商须重选老师（旧绑定回落新厂商默认 + 提示重选，已存选择不覆盖）。

## mimo/（小米预置英文音色，老师的名字就是音色名）

| 文件 | 老师 | 说明 |
|---|---|---|
| `dean.json` | Dean | 默认老师（MiMo 路径） |
| `chloe.json` | Chloe | |
| `milo.json` | Milo | |
| `mia.json` | Mia | |

## zhipu/（智谱 GLM-TTS 预置音色，2026-10-21 接入）

| 文件 | 老师 | 音色 | 默认风格 |
|---|---|---|---|
| `tongtong.json` | Tongtong | tongtong（官方默认） | gentle |
| `xiaochen.json` | Xiaochen | xiaochen | calm |
| `chuichui.json` | Chuichui | chuichui | lively |
| `jam.json` | Jam | jam | lively |
| `kazi.json` | Kazi | kazi（动物圈系） | lively |
| `douji.json` | Douji | douji（动物圈系） | lively |
| `luodo.json` | Luodo | luodo（动物圈系） | lively |

档案只有两样东西，没有任何个人信息：

```json
{
  "teacher_profile": { "display_name": "Chloe" },
  "tts_config": { "voice": "Chloe", "style": "calm" }
}
```

## 为什么在这里，而不在 `data/teachers/`

容器部署时 `data/` 会被 **bind mount 整目录盖住**。如果预置档案只放在
`data/teachers/`，一个空挂载就让它们全部消失，结果每节课都报
`No teacher profiles found`（2026-09-28 阿里云真机两轮复现）。
放在这里会随 `COPY app` 进镜像，**任何挂载都盖不到它**。

`app/teacher.py` 的查找顺序：先读这里的预置，再读 `data/teachers/`——
**同名（同厂商）以你自己那份为准**，你新加的老师也会并进列表
（`data/teachers/{vendor}/` 子目录按厂商归 catalog；旧形态扁平文件视为
mimo）。厂商的合法音色表在 `app/vendors/<厂商>.py` 的 tts 段。

> **别往这里放自己的东西**：程序更新时这个目录会被整体覆盖。
