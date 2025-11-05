# 留言插件（astrbot_plugin_liuyan）

一个用于收集用户留言并回传回复的 AstrBot 插件。

作者：bvzrays

## 功能

- 支持用户在任意会话使用指令 `/留言` 提交留言
- 将留言转发到“固定接收会话”（群/私聊，开发者配置）
- 在接收会话使用指令 `/回复` 可回传回复到原会话
- 自动生成并记录工单号，保障回复路由准确
- 可选以“卡片图片”或“美化文本”两种样式展示留言/回复

## 安装

将本插件目录放到 AstrBot 的 `data/plugins` 下，或通过 AstrBot 插件市场安装。

## 配置

WebUI → 插件管理 → 留言插件 → 配置。

支持的配置项（见 `_conf_schema.json`）：

- render_image（bool，默认 false）
  - false：发送“美化文本”
  - true：发送“图片卡片”（HTML 渲染）
- send_to_users（bool，默认 true）
  - 是否向开发者个人(好友)列表分发
- send_to_groups（bool，默认 true）
  - 是否向开发群列表分发
- platform_name（string，可选，默认 aiocqhttp）
  - 目标平台适配器标识；Napcat 请选择 `aiocqhttp`
- developer_user_ids（list[string]）
  - 开发者QQ号列表（纯数字），如 `123456`
  - 私聊会话在部分适配器下既可写为 `friend` 也可写为 `private`，插件会自动同时尝试两种格式
- developer_group_ids（list[string]）
  - 开发群号列表（纯数字），如 `987654321`
- destination_umo（string，兼容单目标，可选）
  - 旧配置兼容：直接填写完整 UMO（与以上列表可叠加）
- platform_name（string，可选）
- target_type（string，可选，group|friend）
- target_id（string，可选）

插件会基于 `platform_name`（默认 `aiocqhttp`）与 `developer_*_ids` 自动拼出 UMO 并分发。Napcat 场景下请保持 `platform_name=aiocqhttp`。也可额外填写 `destination_umo` 以兼容旧配置。

## 指令

- /留言 <内容>
  - 示例：`/留言 我想反馈一个Bug`
  - 机器人会返回：`留言已提交，工单号：xxxxxx`
  - 插件会把留言转发至配置的接收会话，并包含：平台、群号/私聊、来源用户昵称与QQ、工单号、正文

- /回复 <工单号> <内容>
  - 示例：`/回复 a1b2c3d4 已收到，我们会尽快处理`
  - 插件会将该回复回送至该工单对应的原会话

## 展示样式

- render_image = true：
  - 使用 HTML 模板渲染成卡片图片（渐变背景、信息栅格、圆角卡片风格）
- render_image = false（默认）：
  - 发送美化文本，包含分割线、元信息分组、尾注提示

若图片渲染失败，会自动降级为美化文本，保证可用性。

## 数据持久化

- 工单映射存储于：`data/plugin_data/astrbot_plugin_liuyan/mappings.json`
- 插件在初始化时加载，停用/卸载时保存。

## 注意

- 本地静态检查可能提示导入未解析；在 AstrBot 运行环境中会正常工作。
- 如果需要按来源群路由到不同目标、添加工单查询/关闭等扩展功能，可二次开发。

## 参考

- AstrBot 插件开发文档（项目自带 `astrbot开发.txt`）
- QQ 协议端（Napcat/Lagrange）参考官方文档
