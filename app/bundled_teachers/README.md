# 预置教师档案（随代码分发）

每个文件对应小米的一个预置英文音色，**老师的名字就是音色名**，所以不另外发明人设名：

| 文件 | 老师 | 说明 |
|---|---|---|
| `dean.json` | Dean | 默认老师（`app/config.py` 的 `DEFAULT_TEACHER_ID`）|
| `chloe.json` | Chloe | |
| `milo.json` | Milo | |
| `mia.json` | Mia | |

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
**同名以你自己那份为准**，你新加的老师也会并进列表。

> **别往这里放自己的东西**：程序更新时这个目录会被整体覆盖。
