# nova-incus

将 **Incus 系统容器**作为 OpenStack Nova 计算后端的驱动。它是 nova-incus 的现代化续作,面向
OpenStack `stable/2026.1`、Ubuntu Noble、Incus 7.x 与 Python 3.12。

目标是提供"类虚拟机的持久系统容器":租户在容器内拥有完整的发行版环境(systemd、可用
apt/dnf/yum 安装软件),数据在实例重启、Incus 重启、宿主重启后依然存留,同时保持比虚拟机
更高的密度和更快的启动。

> **状态:开发中,尚未生产就绪。** 核心生命周期与引导卷(BFV)迁移主路径已实现并通过真实多
> 节点验证,但仍存在未清的发布阻断项(见下文"当前状态")。

## 特性

- **系统容器生命周期**:创建、查询、启动/停止、重启、删除、控制台/日志、宿主重启恢复、周期性对账,无需 Incus 集群。
- **两种根盘模型**
  - *本地/临时根盘*:根盘由 Nova 管理,大小取自 Flavor `root_gb`,不进入 Cinder。
  - *引导卷(Boot-from-Volume)*:根盘是 Cinder RBD 卷,通过 Incus fork 的 `cephext` 驱动"认领"而非拷贝;卷的生命周期归 Cinder,Nova 管理其 BDM 与挂载状态。
- **共享 Ceph 池零拷贝迁移**:两个独立(非集群)Incus 节点共享同一 RBD 池时,冷迁移以"所有权交接"方式转移同一个卷,不拷贝根盘数据,停机为秒级。
- **数据卷**:通过宿主侧 os-brick 挂载 Cinder 卷(RBD/LINSTOR),支持在线扩容;非特权容器内以 `fuse2fs` 用户态挂载,避免宿主内核解析租户文件系统。
- **磁盘 QoS**:Flavor `quota:disk_*` 与 Cinder front-end QoS 映射为 Incus `unix-block` 的 `limits.read/write`(cgroup v2 `io.max`)。
- **网络**:Neutron ML2/OVN 拥有租户网络;驱动与 os-vif 准备宿主侧 veth 和 OVS `br-int` 端口,Incus 仅接收容器侧接口。
- **Placement**:上报 VCPU/MEMORY_MB/DISK_GB 与 `CUSTOM_INCUS_SYSTEM_CONTAINER` 特征。
- **安全边界**:每个实例强制非特权、隔离 ID 映射、显式 CPU/内存/进程/根盘配额;租户永不获得 Incus、Podman、Ceph、OVN 或宿主 API 访问权限。

## 架构

- 每个计算节点运行一个**独立、非集群**的 Incus 守护进程,由本节点唯一的 `nova-compute` 管理。Incus 在此扮演类似 libvirt 的本地计算后端角色。
- Nova Scheduler 与 Placement 负责跨节点调度与资源认领;Neutron ML2/OVN 负责租户网络、IPAM、安全组、跨节点 Geneve 连通性;Cinder 负责数据卷(及 BFV 根盘)的生命周期。
- OpenStack 身份(Keystone/Nova/Placement/Neutron/Glance/Cinder)是权威租户边界;Incus 不独立管理租户 OVN 网络,也不作为第二套租户控制面。

详细设计见 [`doc/source/architecture.rst`](doc/source/architecture.rst),部署与用法见
[`doc/source/usage.rst`](doc/source/usage.rst)。

## 依赖的 Incus fork

生产 BFV 与零拷贝迁移依赖 Incus 服务端的 fork([`fivetime/incus`](https://github.com/fivetime/incus)),
它提供以下 API 扩展:

- `storage_ceph_rbd_image_prefix` —— 多个独立节点安全共享一个 RBD 池的镜像缓存前缀。
- `migration_shared_ceph_storage` —— 共享 Ceph 池的零拷贝迁移交接。
- `storage_driver_cephext` —— 认领外部(Cinder)RBD 作为容器根盘。
- `unix_block_limits` —— `unix-block` 设备 I/O 限速(已提交上游 PR)。
- `migration_stateful_shifted_root` —— CRIU restore 使用可迁移的 shifted 非特权 rootfs。

生产 BFV 镜像从 fork 的 `docker/alpine-novm/Dockerfile` 构建,发布为
`ghcr.io/fivetime/incus:alpine-novm`。通用 Incus 镜像不含上述扩展,**不得**作为 BFV 计算镜像使用。

## 在 DevStack 上开发

驱动优先在上游 DevStack 环境中开发调试(`nova-compute`、Incus、OVS、OVN 等以宿主原生服务运行)。
在 `local.conf` 中启用插件:

```
[[local|localrc]]
enable_plugin nova-incus https://github.com/fivetime/openstack-incus
```

如需使用本地代码树,可将其同步到 `/opt/openstack-incus-src` 后从该处开发。可选的 Cinder
Ceph/LINSTOR 后端、Ceph 备份服务等 DevStack 变量见 `devstack/` 与
[`doc/source/usage.rst`](doc/source/usage.rst)。

## 当前状态

核心生命周期、BFV 引导/销毁、BFV 零拷贝迁移的正向 confirm 与反向 revert、以及多种故障注入恢复
场景均已在真实多节点环境通过。达到生产就绪前的主要待办:

- BFV 迁移在每个所有权转换点的**系统化故障注入 + Nova/Cinder/Neutron 自动对账**(发布阻断项)。
- 在统一的 **Noble / Python 3.12** 三节点上跑完整测试套件(当前为混合版本,仅证明兼容性)。
- CRIU 热迁移仅对满足严格预检的系统容器尽力而为；OpenStack API/OVN E2E 通过前不标记为生产支持。
- 有意不支持的能力(本地根盘撤离、rescue、multiattach、加密卷、临时盘)以显式报错声明,而非静默模拟。

逐项功能完成度与最新验证记录见 [`AGENTS.md`](AGENTS.md)(稳定规范)与
[`TEST_STATUS.md`](TEST_STATUS.md)(动态测试进度)。

## 测试

```
tox -e py312     # 单元测试
tox -e pep8      # 代码风格
```

Tempest 场景测试位于 `nova_incus_tempest_plugin/`;真实环境的端到端脚本位于 `tools/`
(Ceph/BFV/迁移/卷/快照/备份的 E2E,以及生产准入 preflight)。破坏性与故障注入脚本必须从
独立的可信编排端运行,切勿将 VM SSH 私钥拷贝到云节点。

## 说明

代码模块路径为 `nova/virt/incus/`,主类为 `IncusDriver`,分发包名为 `nova-incus`,
计算服务入口点为 `nova-incus-compute`。自 nova-lxd 继承的历史命名已全部改完。
