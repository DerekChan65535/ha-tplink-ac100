# Home Assistant TP-Link AC100 集成

面向 **TP-LINK TL-AC100** 控制器的社区自定义集成。直接访问控制器的本地管理接口，
提供 Home Assistant 原生设备、实体和操作。不嵌入网页，不需要云账号、MQTT 桥接或独立服务。

[English](README.md) · [版本发布](https://github.com/DerekChan65535/ha-tplink-ac100/releases) · [报告问题](https://github.com/DerekChan65535/ha-tplink-ac100/issues) · [安全说明](SECURITY.md)

> **早期版本。** 这不是 TP-LINK 或 Home Assistant 官方集成。
> 下列设备/固件组合已验证只读轮询；管理操作是可选功能，**尚未进行真机写入验证**。
> 不要在生产 Wi-Fi 网络上试验重启操作。

## 功能与兼容性

| 项目 | 范围 |
| --- | --- |
| Home Assistant | 最低 **2026.11.1**；开发/测试目标为 2026.11.1。不支持旧版。 |
| 控制器 | 只读验证：**TL-AC100 硬件 5.0**，固件 **1.0.5 Build 250814 Rel.82234n**。其他版本未验证。 |
| 监控 | 以原生实体呈现控制器、受管 AP 和已连接客户端状态。 |
| AP LED | 可选管理功能；依据接口证据实现，未做真机写入验证。 |
| AP/控制器重启 | 可选且会中断网络；**未在真机执行验证**。 |
| SSID/无线参数修改 | 首版不提供。 |
| Debug level logging | 原生诊断开关，默认关闭，独立于设备管理选项。 |
| 默认轮询 | **60 秒**。 |

最低 HA 版本是有意限定：集成直接使用新 API，不提供旧版兼容层。更高版本的 HA 或设备固件
仍可能改变接口；升级前应阅读发布说明，不将“最低版本”理解为已验证所有未来版本。

**管理选项默认关闭，重启按钮实体也默认禁用。** 只有接受风险后才启用管理选项；
启用不代表操作已经安全。不要用写操作探活。模拟测试通过或找到接口源码证据，
都不等于已在设备上安全执行。客户端名称、地址和在线状态可能涉及隐私；
请检查这些实体是否进入历史、仪表盘、自动化或外部服务。

## 控制器管理会话

实测固件同时只允许 **一个管理会话**。即使关闭可选管理操作，只读监控也会占用这个登录会话。

- **添加集成前，先退出其他 AC 管理端**，包括 AC 网页管理界面。
- HA 条目连接期间持续持有会话。如需打开 **AC 网页管理**，必须先在 HA 中
  **禁用该条目（不是删除）**。网页操作完成后退出网页管理，再启用 HA 条目。
- 在 HA 内修改连接地址可使用条目的 **重新配置/Reconfigure**，不必手动预先禁用。
  重新配置/重新认证流程会在验证前临时卸载本条目，不影响其他条目；验证失败、
  连接到错误设备或取消流程时，会自动恢复原条目。
- **忙碌/会话被占用不等于密码错误**。等待其他会话退出或超时后重试，不要强制踢出其他管理员。

配置验证完成后会释放验证所用的临时会话；条目卸载/reload 只释放自己持有的 token，
不会使用 `force=1`，也不会主动踢掉其他管理员。

## 通过 HACS 安装

本仓库可作为 **HACS 自定义仓库**添加。这不等于已进入 HACS 默认目录，
也不代表官方背书或安全审计。

1. 备份 Home Assistant，阅读[目标版本说明](https://github.com/DerekChan65535/ha-tplink-ac100/releases)。
2. 打开 **HACS → 右上角 ⋮ → Custom repositories/自定义仓库**。
3. 输入 `https://github.com/DerekChan65535/ha-tplink-ac100`，类型选 **Integration/集成**，添加。
4. 打开 **TP-Link AC100**，选择 **Download/下载**，选择与 HA 兼容的版本。
5. **重启 Home Assistant Core**。安装/更新 Python 代码后，刷新浏览器、重载 YAML
   或仅重载集成都不能替代这个重启。不需要因此重启宿主机。
6. 打开 **设置 → 设备与服务 → 添加集成 → TP-Link AC100**。
7. 先退出其他 AC 管理端，再填写控制器管理地址和认证信息。先验证监控，保持可选管理操作关闭。

[在 HACS 中打开本仓库](https://my.home-assistant.io/redirect/hacs_repository/?owner=DerekChan65535&repository=ha-tplink-ac100&category=integration)

HACS 下载代码，设备与服务中添加控制器，两者是独立步骤。本项目不要求把 GitHub PAT、
HA 访问令牌或设备密码交给维护者。HACS 本身的安装与配置不在本仓库说明范围内。

## 手动安装

1. 下载某个明确 [Release](https://github.com/DerekChan65535/ha-tplink-ac100/releases) 的源码归档，在本地解压。
2. **只复制**其中的 `custom_components/tplink_ac100/`，放入 HA 配置目录的 `custom_components/`。
3. 最终应能找到 `<HA_CONFIG>/custom_components/tplink_ac100/manifest.json`，
   不要多套一层仓库名或 `custom_components`。
4. 重启 HA Core，然后在设备与服务中添加 **TP-Link AC100**。

HA OS 的配置目录通常是 `/config`。HA Container 则应使用容器 `/config` 对应的宿主机挂载目录，
不要修改 Python `site-packages`。同一安装只选 HACS 或手动一种代码管理方式。

## 更新、回滚和卸载

### 更新

1. 阅读新版本兼容说明，记录当前 HA/集成版本，做完整 HA 备份。包含凭据的备份应妥善保管。
2. 在 HACS 下载更新，或用 **Redownload/重新下载**选指定版本。
   **Update information/更新信息**只刷新元数据，不会安装新代码。
3. 重启 HA Core。先检查监控、实体 ID 和现有自动化，再考虑管理操作。

手动更新时先停止 HA，再用目标版本的完整 `tplink_ac100` 目录替换旧目录，避免残留已删除模块。
旧副本保存在 `custom_components` 外，之后启动 HA。不要覆盖整份 HA 配置目录。

### 回滚

没有不兼容的配置迁移时，可用 **HACS → 仓库 ⋮ → Redownload → Need a different version?**
选择已知良好的旧版，然后重启 HA。旧版不在选择列表时，停止 HA，手动恢复该 Release 的完整目录。

只退代码**不能**撤销 config entry 迁移、HA 升级或已经下发到控制器的配置变更。
这些情况应恢复升级前的完整 HA 备份和匹配的 HA/集成版本。不要手改 `.storage` 强行降级。
重试更新前先确认问题版本的发布说明。

### 卸载

先调整引用该集成的自动化，在设备与服务中删除配置项，再通过 HACS 移除代码
（手动安装则停止 HA 后移除该集成目录），最后重启。
仅移除 HACS 仓库不保证删除集成配置或关联数据。不要删除整个 `custom_components`。

## Debug level logging 调试日志

在控制器的 HA 设备页面使用原生 **Debug level logging** 诊断开关，手动开启或关闭本集成的调试日志，
不必修改 YAML logger 配置。此开关独立于可选设备管理功能，控制器离线时也能切换；
切换**不调用设备 API**。

- 默认**关闭**。状态不持久化，HA 重启后恢复关闭。
- 开启时先保存原级别，再将 `custom_components.tplink_ac100` **及其子 logger** 设为 `DEBUG`，
  不改变 HA root logger 或其他集成的日志级别。
- 关闭时恢复之前的非 `DEBUG` 级别；本命名空间内仍为 `DEBUG` 的 logger 会设为 `INFO`，
  避免子 logger 显式 `DEBUG` 绕过关闭状态。排查结束后请关闭。

日志白名单仅允许固定操作/阶段名称、成功/失败及固定错误类别、耗时和汇总数量。
不会为调试转储原始请求/响应、认证交互、token/cookie/密码、含凭据的 URL，
或设备/客户端名称、MAC/IP 地址、SSID。此开关不是全系统日志脱敏器，分享导出的日志前仍需检查。
详见 [SECURITY.md](SECURITY.md)。

## 网络与凭据安全

- 控制器与 HA 应位于受信管理网络。不要把设备管理端口暴露公网，不为集成开放端口转发。
- 设备支持时使用独立的最小权限账户；若只能使用管理员账号，应理解集成持有相应管理权限。
- HTTP 管理通信没有加密；运行本集成不会消除设备接口自身的安全限制。
  不要以默认关闭 TLS 校验作为通用解决办法。
- HA 配置项和备份可能保存凭据，应保护访问权限。不要上传原始配置、cookie、
  认证响应、抓包或设备数据转储。
- 运行时直接访问本地控制器；安装/升级时 HACS、GitHub 和 Python 包下载可能需要互联网。

漏洞按 [SECURITY.md](SECURITY.md) 私密报告，不要附敏感信息发公开 Issue。
普通故障请提供 HA/集成/硬件/固件版本、复现步骤和**脱敏**日志；不要提供密码或真实客户端数据。

## 开发与发布

使用 Python **3.14.2+** 和 [uv](https://docs.astral.sh/uv/)，在仓库根目录运行：

```console
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest
```

测试必须使用模拟数据，不访问真实控制器或用户 HA。
`uv.lock` 固定 `homeassistant==2026.11.1` 和官方 `pytest-homeassistant==2026.11.1`。

CI 包括项目检查、hassfest 和 HACS validation；它们不证明设备固件兼容或写操作安全。
推送版本 tag 会先执行检查，再创建 GitHub **草稿 Release**，维护者审核后手动发布。
只有 tag 不等于已有可分发 Release。参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可与来源

[MIT](LICENSE)，copyright 2026 Derek Chan and contributors。协议客户端自主实现；
参考第三方接口不代表有权复制无许可源码。见 [THIRD_PARTY.md](THIRD_PARTY.md)。

TP-LINK 和 Home Assistant 名称仅用于说明兼容产品。本社区项目与其权利人无隶属或背书关系。
本仓库许可不授权 TP-LINK 固件、专有网页资源或第三方商标。
