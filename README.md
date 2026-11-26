# SteamGameRename

把 Steam 库里的英文游戏名改成中文——直接改写本机 `appinfo_*.vdf` 缓存，客户端重启后库列表就是中文。

纯本地运行，不联网发请求（除可选的词库更新），不上传任何数据。

## 它解决什么问题

Steam 官方只对部分游戏提供简体中文名，大量独立游戏、老游戏、DLC 在库里仍是英文。改名工具有两类常见做法：改快捷方式（无效，库列表读缓存）或改客户端资源（侵入性大、随更新失效）。

本项目选择第三条路：解析并改写 Steam 的二进制 appinfo 缓存，在字节层面保证除目标名称字段外一个字节都不变。

## 核心特性

**安全底座（这是整个项目的重点）**

- 自研二进制 VDF 解析器，不依赖第三方黑盒库，完整支持 appinfo v42 的字符串池、索引偏移与双层 CRC32
- 写盘前内存二次解析校验；写入前自动备份，失败自动回滚
- 原子写入（临时文件 + `os.replace`），断电不会留下半个分片
- 规则只产出候选，**写入必须人工审批**——自动替换遇到特例很容易改出怪名字

**使用体验**

- 单文件网页界面，支持类型筛选、搜索、批量批准
- 原名对照表：始终显示 Steam 最初的英文原名，悬停可看当前名
- 可选同步简繁中文、写入拼音首字母（让 Steam 库按拼音 A–Z 排序）
- 静默同步：Steam 更新覆盖汉化后，一键补回已批准项
- 桌面版：原生窗口 + 系统托盘，Steam 退出后自动补写

**工程细节**

- 分片解析结果按 `(mtime, size)` 增量缓存，Steam 只改 1 个分片时重算约 20 ms
- 词库（社区 / 商店 / 原名）走 SQLite 按需查询，耗时与词库规模无关
- 台账与词库统一存入 SQLite；旧 JSON 仅在首次迁移时读取并备份，之后不再作为运行数据源
- 可随时用 `export-ledger` 导出可读 JSON 备份，便于查看、diff 和归档

## 快速开始

需要 **Python 3.11+**，**仅支持 Windows**。

程序依赖 Windows 的 `tasklist` 检测 Steam 进程、依赖注册表定位安装目录，并用 `os.replace` 做原子替换。在 Linux/macOS 上即使指定 `--steam-dir` 也会被写入保护拒绝（因为无法确认 Steam 是否在运行），这是有意为之，不是 bug。

```bash
git clone https://github.com/yguangjie365/SteamGameRename.git
cd SteamGameRename
```

**启动**

双击 **`SteamGameRename.bat`**。首次运行会自动安装 `pywebview / pystray / Pillow`，装不上会提示手动安装命令。

界面是独立窗口，关闭只是收进托盘；Steam 退出后会自动补写被覆盖的汉化。

**命令行**

```bash
python src/cli.py scan              # 列出应用与当前名称
python src/cli.py plan              # 预览改名计划
python src/cli.py apply --execute   # 写入（会先备份）
python src/cli.py sync --execute    # 补回被 Steam 覆盖的汉化
python src/cli.py export-ledger -o names-export.json  # 从 SQLite 导出台账
```

## 使用流程

1. 关闭 Steam（含托盘图标）
2. 打开编辑器，勾选/编辑要改的游戏，点「保存台账」
3. 点「应用写入」——先备份，再改写，最后校验
4. 启动 Steam，库列表显示中文
5. 之后 Steam 更新覆盖了汉化，点「静默同步」或让它自动补写

## 文档

- [使用说明](使用说明.md) — 面向普通玩家，含常见问题
- [开发者说明](docs/开发者说明.md) — 架构、二进制格式、校验策略、实测数据

## 项目结构

```
run_desktop.py      程序入口：pywebview 窗口 + 托盘 + Steam 退出后自动补写
steam_locator.py    Steam 安装目录探测（参数/环境变量/注册表/常见路径/全盘扫描）
src/
  vdf.py            二进制 VDF 编解码
  shard.py          appinfo 分片读写与校验
  mapping.py        名称映射与计划生成
  db.py             SQLite 存储层
  backup.py         备份、原子写入、回滚
  cli.py / gui.py   命令行 / 本地网页服务
  web/index.html    单文件前端
  defaults/         随包模板与词库
tests/              回归测试与真实分片 fixture
```

## 免责声明

本工具会修改 Steam 客户端本地缓存文件。项目已实现备份、CRC/SHA1 校验与失败回滚，但仍建议：

- 写入前手动退出 Steam（不要强杀进程），确保云存档已同步
- 保留自动生成的 `backups/` 目录，出问题可一键恢复
- 不要在有游戏下载/更新的同时写入

因使用本工具导致的任何数据问题，需自行承担。

## 许可

[MIT](LICENSE) © 2026 yguangjie365

可自由使用、修改、分发（含商业用途），保留版权声明与许可声明即可。软件按「原样」提供，不附带任何担保。
