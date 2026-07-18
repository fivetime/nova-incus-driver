# Nova ComputeDriver 源码摸底记录（历史）

> 本文保留最初的 Nova/Incus 驱动差距分析，不再表示当前实现状态。
> 当前权威状态见 `support_matrix/capabilities.json`，真实环境证据见仓库根目录
> `TEST_STATUS.md`。不要把本文中的“待实现”项目重新加入开发计划，除非先与这两个
> 当前来源以及代码、E2E 脚本交叉核对。

本文件是 Incus 计算驱动相对 Nova `ComputeDriver` 契约的权威能力矩阵,基于对
本地 Nova 树(master `4d9d8caff2`,冻结时需对 stable/2026.1 重做签名差异审计)、
libvirt 参考驱动和本项目 `nova/virt/incus/` 的四路源码审计。

**核心原则(来自 manager 编排审计)**:manager 负责编排(task_state/vm_state 状态机、
Cinder BDM/attachment 生命周期、Neutron port 绑定、Placement claim/allocation、quota、
镜像上传编排、evacuate fencing、shelve/unshelve 资源释放与重 claim),**驱动只负责
hypervisor 侧那一个动作**。凡是框架已包办的,驱动一律不重复实现。

图例:FULL 完整实现 · PARTIAL 有偏差/边界 · REJECT 显式拒绝 · FRAMEWORK 框架兜底(驱动无需实现) ·
TODO 需实现 · N/A 系统容器不适用(永久拒绝)。

---

## 第零层:子系统所有权(目录级)

我们的实现范围本质上**只有 `nova/virt/incus/`**(libvirt/ironic/vmwareapi/zvm 的平级兄弟),
外加三个薄接入点。`nova/` 下其余目录全是**共享框架**,消费而不重写。

### 属于我们(实现)

| 路径 | 内容 |
|---|---|
| `nova/virt/incus/` | **驱动主体**:driver.py、storage.py、flavor.py、config.py、vif.py、common.py、session.py、client.py |
| `nova/virt/incus/manager.py` | ComputeManager 的最小子类(仅加 BFV 迁移故障恢复周期任务) |
| `nova/virt/incus/cmd/` | `nova-incus-compute` 薄入口(2026.1 不再用 compute_manager 配置项) |
| `nova/tests/unit/virt/incus/` | 单元测试 |
| `[incus]` conf 组 | 在框架 oslo.config 机制上新增配置项(机制是框架的) |

### 属于框架(消费,绝不重写)

| 目录 | 职责 | 驱动关系 |
|---|---|---|
| `api/` | REST API、microversion、schema、policy 校验 | 零关系,完全框架 |
| `compute/` | **compute manager(调用我们的驱动)**、RPC、ResourceTracker、monitors | 我们被它调用;只最小子类化 |
| `scheduler/` | 调度器、filters、weights、Placement 客户端 | 零关系;我们只经 `update_provider_tree` 上报库存 |
| `conductor/` | build/migrate 任务编排、conductor RPC | 零关系 |
| `db/` | Nova cell/api 数据库模型 + 迁移 | 零关系 |
| `objects/` | 版本化对象(Instance/Migration/BDM/Flavor…) | 消费其数据,不定义 |
| `network/` | Neutron 客户端、网络模型、安全组 | 消费 `network_info`,不碰 IPAM/SG |
| `volume/` | Cinder 客户端、os-brick 加密器封装 | 用 `get_volume_connector` + os-brick attach |
| `image/` | Glance 客户端 | 经 `image_meta`/snapshot 回调间接用 |
| `console/` | 控制台鉴权代理(RFB/serial/securityproxy) | 我们的 `get_*_console` hook 喂给它;代理本身框架 |
| `notifications/` | 版本化通知 | 框架自动发,不管 |
| `policies/` | RBAC 策略默认 | 零关系 |
| `limit/` | 统一配额(Keystone limits) | 零关系 |
| `pci/` | PCI 直通追踪 | 系统容器基本 N/A |
| `servicegroup/` | 服务存活判定(evacuate fencing 的软校验在此) | 零关系,框架判 |
| `keymgr/` | Barbican 密钥(卷加密) | 我们拒绝加密卷 |
| `share/` | Manila share 编排 | `mount_share`/`umount_share` hook(当前拒绝) |
| `storage/` | Nova 自带 ceph 助手(rbd_utils,libvirt 用) | 不用,我们走 Incus cephext |
| `accelerator/` | Cyborg ARQ | 收 `accel_info`,系统容器基本 N/A |
| `privsep/` | 权限分离守护 | 用其机制做少量宿主操作(chown/设备节点),不重写框架 |
| `conf/` | oslo.config 机制 | 在其上加 `[incus]` 组 |
| `cmd/` | 各服务入口 | 加一个薄 `nova-incus-compute` |
| `wsgi/`、`hacking/`、`locale/` | WSGI、lint、翻译 | 零关系 |

**一句话**:凡是"编排、调度、状态机、数据模型、API、鉴权、配额、通知"都是框架;我们只做
"把最终动作落到 Incus + 资源/能力上报 + 宿主侧 VIF/卷执行 + Ceph 根盘所有权转换 + 幂等回滚 +
不适用能力的标准化拒绝"。

---

## 第一层:框架已包办,不属于 Incus 驱动的工作

以下由 Nova 框架完成,驱动**不得**重复实现(只需保证被调用的那几个通用 hook 正确):

| 操作 | 框架职责 | 驱动只需提供的 hook |
|---|---|---|
| rebuild | `_rebuild_default_impl`(manager.py:3796)= detach → destroy → spawn | `destroy` + `spawn`(无需 `def rebuild`) |
| evacuate | `rebuild(recreate=True)`;fencing 在 **API 层**(api.py:5727 校验源 service down,真正 STONITH 由外部 Masakari/IPMI);目标端 rebuild_claim + finish_evacuation 切 host;源端恢复后 `_destroy_evacuated_instances`(manager.py:893)清残留 | `supports_evacuate=True` + `instance_on_disk`(对共享 Ceph 返 True)+ `destroy` + `spawn` |
| shelve/unshelve | Placement 释放/重 claim、host 置空/重设、Cinder attachment、port binding 全在 manager | `power_off` + `snapshot` + `destroy` + `spawn`(无专用 hook) |
| resize/冷迁移 | task_state、Cinder attachment 删建、port binding host 切换、Placement move-claim | `migrate_disk_and_power_off`/`finish_migration`/`confirm_migration`/`finish_revert_migration` |
| 卷 attach/detach | Cinder attachment 生命周期、BDM、回滚编排 | `attach_volume`/`detach_volume`(经 BDM 对象间接调用) |
| interface attach/detach | Neutron port 分配/释放、PCI claim、Placement | `attach_interface`/`detach_interface` + `network_binding_host_id` |
| 电源状态同步 | DB 对账、意外关机走 compute_api.stop/delete | `get_num_instances` + `get_info` |
| pause/suspend/快照 | 通知 + 状态机;snapshot 的 image 生命周期与失败删除 | 对应 hypervisor 动作 hook |

**结论**:rebuild、evacuate、shelve/unshelve **都不是要新写的驱动方法**——它们由框架用
`destroy`/`spawn`/`power_off`/`snapshot` 组合而成。这把"最大生产缺口 evacuate"从"实现大方法"
降级为"打开能力位 + 修 `instance_on_disk` + 保证 destroy/spawn 在共享 Ceph 下正确 + 外部 fencing"。

---

## 第二层:已实现(区分已验证 / 待补 E2E)

| 方法 | 状态 | 行号 | E2E 证据 |
|---|---|---|---|
| spawn(普通根盘 + BFV) | FULL | driver.py:810 | ✅ 多节点已验证 |
| destroy / cleanup | FULL | 991 / 1030 | ✅ |
| reboot | FULL | 1140 | ✅ |
| power_on/off | FULL | 1742 / 1732 | ✅ |
| get_info | FULL | 778 | ✅(修过 stopped 容器 Invalid PID -1) |
| list_instances / _uuids | FULL | 792 / 798 | ✅ |
| snapshot→Glance | FULL | 1639 | ✅ |
| attach/detach_volume | FULL | 1300 / 1377 | ✅(拒绝加密/ro/multiattach) |
| extend_volume | PARTIAL | 1483 | ✅ |
| attach/detach_interface | FULL | 1499 / 1518 | ✅ |
| migrate_disk_and_power_off | PARTIAL | 1550 | ✅ 正向 confirm |
| finish_migration | PARTIAL | 1932 | ✅ |
| confirm_migration | FULL | 2066 | ✅ |
| finish_revert_migration | PARTIAL | 2074 | ✅ 反向 revert |
| update_provider_tree | FULL | 1828 | ✅ |
| get_available_resource | PARTIAL | 1753 | ⚠️ vcpus_used 恒为 0(见缺口) |
| plug/unplug_vifs | FULL | 1889 / 1905 | ✅ |
| get_console_output | FULL | 1288 | ✅ |
| resume_state_on_host_boot | FULL | 1705 | ✅ 宿主重启恢复 |
| BFV cephext 迁移交接 + 故障恢复 | PARTIAL | 多处 | ✅ 5 类故障注入 |

**已实现但缺当前三节点公共 API E2E**(你战略计划已列):pause/unpause、config-drive、
metadata/keypair/user-data、BFV rebuild/reimage、SHUTOFF resize 全矩阵、Flavor swap、部分 QoS 组合。

---

## 第三层:尚需实现 / 修正(按依赖顺序)

### A. 语义与正确性修正(低风险,应尽快)

| 项 | 现状 | 应改为 | 行号 |
|---|---|---|---|
| suspend/resume | **已 REJECT**(审计确认已改) | 保持 REJECT,契约正确 ✅ | 1694 / 1699 |
| pause/unpause | freeze/unfreeze | 保持(pause 契约允许保留内存,合规) | 1676 / 1685 |
| `get_available_resource.vcpus_used` | **恒为 0** | 按实际运行实例 vCPU 汇总,否则 Placement 调度误判超卖 | 1808 |
| capabilities 缺声明 | BFV 已支持但未开 `supports_bfv_rescue` 等位;`supports_multiattach`/device_tagging 未显式 | 显式声明每个 supports_*(True/False/N-A),消除隐式默认 | 724-730 |
| live-migration 死代码 | pre/post/post_at_source/cleanup_check 有实体但 live_migration+check 全 REJECT → 不可达 | 删除不可达实体或加注释说明为 evacuate 预留 | 2123-2161 |
| `cleanup_lingering_instance_resources` | 生产零调用(仅单测) | 确认是否该接入 manager 清理路径,否则删 | 1087 |
| 遗留 XXX/NOTE | driver.py:1924/2169 未清理区、1782 ZFS 遗留上报 | 清理或明确标注 | 多处 |

### B. 计量能力(收入相关,建议提前)

| 方法 | 现状 | libvirt 参考 | 优先级 |
|---|---|---|---|
| `get_instance_diagnostics` | 未实现(基类 raise) | driver.py:13274 | 高 |
| `block_stats` | 未实现 | driver.py:9769 | 高 |
| `get_all_volume_usage` | 未实现 | 复用 block_stats(9734) | 中 |

`get_all_bw_counters` **已从 Nova 接口移除**,无需实现。计量缺口导致 Ceilometer/计费不可用。

### C. evacuate(生产 HA 缺口,依赖迁移收尾)

**不是新写方法**,而是:①`supports_evacuate=True`;②`instance_on_disk` 对共享 Ceph pool 返 True
(关键,否则 manager 误判本地盘);③保证 `destroy`/`spawn` 在"源端失联"下正确;④外部 fencing
(Nova 只做 service-down 软校验);⑤本地根盘实例明确拒绝 evacuate。**依赖**:先完成普通 BFV 迁移的
post-claim 系统化故障注入(硬化 fence/claim/reconcile 原语),evacuate 复用这套原语。

### D. 永久拒绝(系统容器不适用)——优雅拒绝路径核对

`get_vnc_console`/`get_spice_console`/`get_mks_console`(VM 图形协议)、`trigger_crash_dump`
(无 guest 内核)、`quiesce`/`unquiesce`(依赖 qemu-guest-agent fsfreeze)、`live_migration` 全家桶
(需 CRIU,当前不做)、以及 capabilities 里 `supports_vtpm`/`supports_secure_boot`/UEFI/
`supports_mem_backing_file`/几乎全部 `supports_image_type_*`。

**关键**:拒绝的操作**不会从 API/CLI 消失**——端点始终存在,用户仍可调用。优雅程度取决于
API 控制器是否 catch `NotImplementedError`。已逐个核对(证据:nova/api/openstack/compute/):

`common.raise_feature_not_supported()` → HTTP **501**(common.py:485)。

| 驱动方法 | API 动作 / 控制器 | API 是否 catch NotImpl | 裸 raise 的实际结果 | 优雅? | 需要的动作 |
|---|---|---|---|---|---|
| `get_vnc_console` | server vnc / remote_consoles.py:68 | ✅ →raise_feature_not_supported | 501 | ✅ 自动干净 | 不实现即可 |
| `get_spice_console` | remote_consoles.py:97 | ✅ | 501 | ✅ | 不实现 |
| `get_serial_console` | remote_consoles.py:140 | ✅ | 501 | ✅ | 不实现 |
| `get_mks_console` | remote_consoles.py:185 | ✅ | 501 | ✅ | 不实现 |
| `set_admin_password` | server set-password / admin_password.py:59 | ✅ | 501 | ✅ | 不实现 |
| `live_migration` | server migrate --live | ✅ 经 `check_can_live_migrate_source` 抛 `MigrationPreCheckError`→400(migrate_server.py:153) | 400,先于 live_migration 拒绝 | ✅ | 保持现状(check 已 REJECT) |
| **`suspend`** | server suspend / suspend_server.py:37 | ❌ **不 catch** | manager 在 `_error_out_instance_on_exception`(manager.py:7457)内调用 → **实例进 ERROR + 500** | ❌ **丑** | **FIX**:改抛 API 能识别的异常(见下) |
| **`resume`** | server resume / suspend_server.py:63 | ❌ 不 catch | 同上 500 | ⚠️ | 只有 SUSPENDED 才可达;suspend 拒绝后不可达,低优先 |
| **`rescue`** | server rescue / rescue.py:46 | ❌ 不 catch NotImpl(但 catch `InstanceNotRescuable`→400,rescue.py:77) | 裸 NotImpl → 500 | ❌ **丑** | **FIX**:改抛 `InstanceNotRescuable` → 干净 400 |
| **`unrescue`** | server unrescue / rescue.py:92 | ❌ 不 catch | 500 | ⚠️ | rescue 拒绝后不可达,低优先 |
| `trigger_crash_dump` | server trigger-crash-dump / servers.py:1581 | ❌ 不 catch NotImpl(仅 catch InstanceNotReady/IsLocked/InvalidState) | 裸 NotImpl → 500 | ⚠️ 丑但极少被调 | 低优先;需要时同 suspend 修法 |
| `quiesce`/`unquiesce` | 内部(assisted volume snapshot 路径) | 非直接用户动作;由 snapshot 能力决定是否调用 | — | — | 不声明支持即不被调用 |

**item 2 执行结论**:
- **自动干净(501)**:VNC/SPICE/serial/MKS console、set_admin_password——只要不实现,API 层已 catch,零工作。
- **已干净(经 check 前置拒绝)**:live migration 全家桶——`check_can_live_migrate_source` 抛
  `MigrationPreCheckError`,在 `live_migration` 被调前就 400,现状正确。
- **需修(会返 500 + 脏 ERROR 态)**:`suspend`、`rescue` 目前裸抛 `NotImplementedError`,而其 API 控制器
  不 catch 它。修法:`rescue` 改抛 `exception.InstanceNotRescuable`(rescue.py 已 catch→400);`suspend`
  无对应能力位与专用异常,需选一个 API 能识别的异常(如 `InstanceInvalidState`→409,附"系统容器不支持
  挂起"说明)或评估在 compute/api.py 前置拒绝。`resume`/`unrescue` 在其前置动作被拒后不可达,低优先。

### 控制台访问模型(已定,无需新增工作)

系统容器无虚拟显卡/帧缓冲,不存在 VM 式的图形控制台。文本控制台按三分法处理:

| 能力 | 机制 | 状态 | 归属 |
|---|---|---|---|
| 控制台日志(只读) | `get_console_output`(driver.py:1288)→ `container.console_log()` 末 100KiB → `openstack console log show` | ✅ 已实现 | 驱动 |
| 交互式登录 | 镜像预装 sshd,挂租户 Neutron 网络,经浮动 IP + 安全组访问 | ✅ 应用级(镜像打包) | 租户/镜像,驱动不参与 |
| 交互式串口控制台 | `get_serial_console` → nova-serialproxy | ❌ **不实现**;未覆盖 → API 层 catch(remote_consoles.py:140)→ 干净 501 | 无需(SSH 已覆盖交互) |

**救援价值(关键)**:系统容器不支持 VM 式 rescue(`rescue` REJECT),因此当 SSH 不可达(配置错误、
网络故障、启动失败)时,`openstack console log show` 是**唯一的兜底诊断通道**——能看到 boot 消息与
失败原因,无需进入实例。控制台日志实际承担了"保姆级"容器的主要救援/排障职责,而非可有可无的功能。

**镜像约定**:`console_log()` 返回容器 `/dev/console` 内容,取决于镜像 init 是否把启动日志与
getty 输出送到 `/dev/console`(systemd/openrc 默认如此)。镜像流水线需验证一次,同"顶层 rootfs/ 布局"
一类的镜像约定。

### E. 按产品需求再定(高级能力边界)

`rescue`/`unrescue`(当前 REJECT,未来需 storage-pool-native rootfs)、`get_serial_console`、
`set_admin_password`、`mount_share`/`umount_share` (Manila) 和 config-drive
均已实现并完成真实 API E2E。Manila 验证覆盖 Nova 2.97 映射、Manila
access rule、Podman mount propagation、容器内读写以及完整卸载清理。

### F. `swap_volume` —— REJECT(数据安全,待冷交换协议)

Nova `swap_volume`(Cinder retype/migrate 触发)契约要求在运行中把旧卷数据 **live block-copy**
到新卷再切换——libvirt 靠 QEMU `drive-mirror`。容器**无等价的运行时块镜像机制**(和"无 suspend-to-disk
需 CRIU"同类结构性缺失)。原实现只改 profile 设备指针、从不拷贝数据 → 新卷全零,**静默数据丢失**
(真实 Ceph→LVM retype 已证实)。

现状(commit `5f46338`):`swap_volume` 在任何连接/profile 修改前 `raise NotImplementedError`,
**fail-closed**。真实 retype 验证:Cinder retype 失败并回滚、原卷/UUID/attachment 全保留、
前后 SHA-256 一致、临时目标清理。对应 `capabilities.json` 的 `cinder-volume-swap` = `unsupported`。

**未来做完整冷交换需**(与 BFV 迁移交接同级严谨度,可复用其 handover 状态机 + 故障恢复标记):
持久化阶段状态、nova-compute 崩溃恢复、双 attachment 清理、复制校验(checksum)、Cinder confirm
前后的回滚协议。在此之前,明确拒绝是唯一生产安全行为。

**待确认**:manager `_swap_volume` 对 `NotImplementedError` 的处理是否会污染实例态(如置 ERROR)。
E2E 显示 attachment 保留、卷回滚、实例未受影响;若 manager 有实例置 ERROR 分支,评估换更友好的异常。

---

## 建议的对齐顺序

1. **能力矩阵**(本文件)—— 建立 source of truth,持续更新。
2. **语义修正(A)** —— suspend 已对;修 `vcpus_used`、显式化 capabilities、清死代码。低风险快赢。
3. **Tempest 现代化** —— 覆盖已声明支持的功能(先堵"实现了但未验证"),去掉旧 Incus 假设(default project/bridged NIC/ephemeral)。
4. **计量(B)** —— `get_instance_diagnostics` + `block_stats`,打通计费。
5. **迁移 post-claim 系统化故障注入** —— 硬化 fence/claim/reconcile(现有 production blocker)。
6. **evacuate(C)** —— 复用第 5 步原语 + `instance_on_disk` + 外部 fencing 设计 + 断电测试。
7. **公共 API E2E 补齐** —— shelve/unshelve、BFV rebuild、resize 全矩阵、pause、config-drive、metadata。
8. **冻结前** —— 对最终 stable/2026.1 做接口签名差异审计。
