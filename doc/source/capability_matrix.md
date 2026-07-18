# Nova Incus 当前能力审计

本文件说明如何判断一项能力是否完成，避免从历史计划恢复会话时重复开发。

## 事实来源

按以下优先级核对：

1. `support_matrix/capabilities.json`：当前支持、框架提供、实验或拒绝状态。
2. 仓库根目录 `TEST_STATUS.md`：带日期、节点、资源和故障场景的实测证据。
3. `tools/openstack-incus-*-e2e.sh`：可重复执行的发布门禁。
4. `nova/tests/unit/virt/lxd/` 和 `nova/virt/lxd/`：接口契约与实现。
5. `capability_matrix_historical.md`：仅用于理解早期差距，不是待办清单。

只有“代码、自动化门禁、实测证据”三者均缺失时，才应把项目重新列为未完成。

## 已闭环

- Nova 实例生命周期以及 Neutron OVN/OVS 网络。
- Cinder 数据盘和 Ceph RBD boot-from-volume。
- BFV 无拷贝冷迁移、confirm、revert，以及全部有序计算节点对。
- 迁移 pre-claim/post-claim、数据盘、目标启动、停止实例和反向 revert
  故障注入与残留资源审计。
- BFV rebuild/reimage、shelve/unshelve、pause/unpause、跨节点 resize、
  在线根卷扩容和 config-drive 保持。
- metadata、keypair、user-data、console log、diagnostics 和卷 I/O 计量。
- 三节点 production preflight、fleet drift audit、镜像 digest 和 Incus fork
  revision 校验。
- BFV failed-host evacuation 的 Nova/Cinder/Neutron/Incus 数据路径已经过人工
  隔离源端后的真实 E2E：根盘标记、固定 IP、Placement、单一 RBD watcher 和
  源恢复清理均通过。
- suspend/resume/rescue/unrescue 在 compute manager 中无副作用拒绝，恢复
  task state、保留 VM state，并写入明确的 Nova action event。

## 有意拒绝

- CRIU live migration。
- 本地根盘 failed-host evacuation。
- suspend-to-memory、rescue、guest kernel crash dump。
- 图形控制台以及 VM 固件、vTPM、Secure Boot、PCI/NUMA 加速能力。
- Cinder 在线 volume swap/retype、只读、加密和 multiattach 数据盘。

这些项目不能因为 API 端点存在就被视为驱动缺陷。必须保持显式、无数据破坏的
拒绝语义，除非产品边界改变并新增完整设计与验证。

## 唯一生产 HA 缺口

BFV evacuation 的驱动路径和人工 fencing E2E 已完成，但生产自动化仍缺外部
STONITH/电源隔离集成。停止 Podman 内的 incusd 不能隔离宿主上的 LXC monitor、
挂载、KRBD 映射或 Ceph watcher。

生产启用 `allow_bfv_evacuate` 前，外部系统必须：

1. 证明源计算节点已断电或无法访问 Ceph。
2. 确认目标 RBD 没有源端 watcher。
3. 再调用 Nova evacuation。
4. 以 Nova terminal task state 和原始电源状态判断完成，不能假定总是 ACTIVE。

该能力在 `capabilities.json` 中保持 `experimental`，直到上述外部 fencing
接入发布自动化并完成断电 E2E。迁移矩阵和三节点生产审计不需要重做开发，只需
作为每个版本的回归门禁重新执行。
