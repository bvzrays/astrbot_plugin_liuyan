from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import os
import json
import uuid
import asyncio


@register("astrbot_plugin_liuyan", "bvzrays", "留言插件：/留言 与 /回复", "1.0.0")
class LiuyanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig | None = config
        self._ticket_map: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._data_dir = self._ensure_data_dir()
        self._mapping_path = os.path.join(self._data_dir, "mappings.json")

    async def initialize(self):
        """初始化时加载历史映射。"""
        await self._load_mappings()

    async def terminate(self):
        """插件销毁时保存映射。"""
        await self._save_mappings()

    # /留言 <内容>
    @filter.command("留言")
    async def cmd_liuyan(self, event: AstrMessageEvent):
        message = event.message_str.strip()
        if not message:
            yield event.plain_result("用法：/留言 你的留言内容")
            return

        dest_umos = self._get_destination_umos()
        if not dest_umos:
            yield event.plain_result("未配置留言接收目标，请在配置中设置 destination_umo 或开发者/开发群列表")
            return

        ticket = uuid.uuid4().hex[:8]
        sender_name = event.get_sender_name() or ""
        sender_id = event.get_sender_id() or ""
        group_id = event.get_group_id() or ""
        platform_name = event.get_platform_name() or ""

        # 记录映射
        async with self._lock:
            self._ticket_map[ticket] = {
                "umo": event.unified_msg_origin,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "group_id": group_id,
                "platform": platform_name,
            }
        await self._save_mappings()

        # 组织转发页面（HTML 渲染为图片）
        origin_info = {
            "ticket": ticket,
            "platform": platform_name,
            "group_id": group_id or "私聊",
            "sender_name": sender_name,
            "sender_id": sender_id,
            "content": message,
        }

        try:
            sent_any = False
            if self._should_render_image():
                img_path = await self._render_leaving_card(origin_info)
                chain = MessageChain().file_image(img_path)
            else:
                chain = MessageChain().message(self._format_liuyan_text(origin_info))
            for umo in dest_umos:
                try:
                    ok = await self.context.send_message(umo, chain)
                    sent_any = sent_any or bool(ok)
                except Exception as _:
                    continue
            if not sent_any:
                raise RuntimeError("所有目标发送失败或未找到对应平台")
            yield event.plain_result(f"留言已提交，工单号：{ticket}")
        except Exception as e:
            logger.error(f"转发留言失败: {e}")
            # 降级为纯文本
            fallback = self._format_liuyan_text(origin_info)
            try:
                chain = MessageChain().message(fallback)
                for umo in dest_umos:
                    try:
                        await self.context.send_message(umo, chain)
                    except Exception as _:
                        pass
            except Exception as _:
                pass
            yield event.plain_result("留言转发失败，请稍后再试或联系管理员。")

    # /回复 <工单号> <内容>
    @filter.command("回复")
    async def cmd_reply(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if not text:
            yield event.plain_result("用法：/回复 工单号 内容")
            return

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：/回复 工单号 内容")
            return

        ticket, reply_text = parts[0], parts[1].strip()
        async with self._lock:
            mapping = self._ticket_map.get(ticket)

        if not mapping:
            yield event.plain_result("未找到该工单号，请检查后再试。")
            return

        dest_umo = mapping.get("umo")
        sender_name = mapping.get("sender_name", "")
        sender_id = mapping.get("sender_id", "")

        back_data = {
            "ticket": ticket,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "content": reply_text,
        }

        try:
            if self._should_render_image():
                img_path = await self._render_reply_card(back_data)
                chain = MessageChain().file_image(img_path)
            else:
                chain = MessageChain().message(self._format_reply_text(back_data))
            ok = await self.context.send_message(dest_umo, chain)
            if ok is False:
                raise RuntimeError("send_message 返回 False，未找到对应平台")
            yield event.plain_result("已回送给留言用户。")
        except Exception as e:
            logger.error(f"回复回送失败: {e}")
            # 降级为纯文本
            fallback = self._format_reply_text(back_data)
            try:
                chain = MessageChain().message(fallback)
                await self.context.send_message(dest_umo, chain)
            except Exception as _:
                pass
            yield event.plain_result("回复发送失败，请稍后再试。")

    def _get_destination_umos(self) -> list[str]:
        """根据配置获取目标会话列表：
        - 使用开发者QQ/群号列表自动拼 UMO（{platform}:friend:QQ / {platform}:group:GID）；
        - 兼容单一 destination_umo；
        """
        results: list[str] = []
        if not self.config:
            return results
        platform = (self.config.get("platform_name", "") or "").strip() or "aiocqhttp"
        try:
            if bool(self.config.get("send_to_users", True)):
                user_ids = self.config.get("developer_user_ids", []) or []
                for uid in user_ids:
                    if isinstance(uid, str) and uid.strip():
                        results.append(f"{platform}:friend:{uid.strip()}")
            if bool(self.config.get("send_to_groups", True)):
                group_ids = self.config.get("developer_group_ids", []) or []
                for gid in group_ids:
                    if isinstance(gid, str) and gid.strip():
                        results.append(f"{platform}:group:{gid.strip()}")
        except Exception:
            pass

        # 兼容：单一 UMO
        dest = (self.config.get("destination_umo", "") or "").strip()
        if dest:
            results.append(dest)

        # 去重
        seen = set()
        dedup = []
        for x in results:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
        if not dedup:
            logger.warn("留言插件未得到任何目标会话（请检查 platform_name / developer_user_ids / developer_group_ids / destination_umo 配置）")
        else:
            logger.info(f"留言插件目标会话: {dedup}")
        return dedup

    def _ensure_data_dir(self) -> str:
        """确保 data 下的插件数据目录存在。"""
        # 运行目录一般为 AstrBot 根目录
        base = os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_liuyan")
        os.makedirs(base, exist_ok=True)
        return base

    async def _load_mappings(self):
        try:
            if os.path.exists(self._mapping_path):
                with open(self._mapping_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._ticket_map = data
        except Exception as e:
            logger.error(f"加载映射文件失败: {e}")

    async def _save_mappings(self):
        try:
            tmp_path = self._mapping_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._ticket_map, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._mapping_path)
        except Exception as e:
            logger.error(f"保存映射文件失败: {e}")

    def _should_render_image(self) -> bool:
        try:
            if not self.config:
                return False
            v = self.config.get("render_image", False)
            return bool(v)
        except Exception:
            return False

    def _format_liuyan_text(self, data: dict) -> str:
        line = "────────────────────────────────────────"
        return (
            f"[留言工单] {data.get('ticket','')}\n"
            f"{line}\n"
            f"来源平台：{data.get('platform','')}\n"
            f"来源群号：{data.get('group_id','私聊')}\n"
            f"来源用户：{data.get('sender_name','')} ({data.get('sender_id','')})\n"
            f"{line}\n"
            f"内容：\n{data.get('content','')}\n"
            f"{line}\n"
            f"使用 /回复 {data.get('ticket','')} 内容 进行回复"
        )

    def _format_reply_text(self, data: dict) -> str:
        line = "────────────────────────────────────────"
        return (
            f"[留言回复] 工单 {data.get('ticket','')}\n"
            f"{line}\n"
            f"回复给：{data.get('sender_name','')} ({data.get('sender_id','')})\n"
            f"{line}\n"
            f"内容：\n{data.get('content','')}"
        )

    async def _render_leaving_card(self, data: dict) -> str:
        """将留言数据渲染为图片并返回本地路径。"""
        tmpl = self._liuyan_template()
        path = await self.html_render(tmpl, data, return_url=False, options={
            "type": "png",
            "omit_background": True,
            "full_page": True
        })
        return path

    async def _render_reply_card(self, data: dict) -> str:
        """将回复数据渲染为图片并返回本地路径。"""
        tmpl = self._reply_template()
        path = await self.html_render(tmpl, data, return_url=False, options={
            "type": "png",
            "omit_background": True,
            "full_page": True
        })
        return path

    def _liuyan_template(self) -> str:
        return (
            """
<div style="width: 680px; padding: 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Helvetica Neue', Arial, 'Noto Sans SC', 'Microsoft YaHei', sans-serif; background: linear-gradient(180deg,#ffffff 0%, #f7f9ff 100%); color: #1f2937; border-radius: 16px; border: 1px solid #e5e7eb; box-shadow: 0 10px 30px rgba(15,23,42,0.08);">
  <div style="display:flex; align-items:center; gap:10px; margin-bottom: 12px;">
    <div style="width:10px; height:10px; background:#3b82f6; border-radius:50%"></div>
    <div style="font-weight:700; font-size:18px; color:#111827">留言工单 {{ ticket }}</div>
  </div>

  <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; font-size: 13px; color:#374151">
    <div><span style="color:#6b7280">来源平台：</span>{{ platform }}</div>
    <div><span style="color:#6b7280">来源群号：</span>{{ group_id }}</div>
    <div><span style="color:#6b7280">来源用户：</span>{{ sender_name }}</div>
    <div><span style="color:#6b7280">来源QQ：</span>{{ sender_id }}</div>
  </div>

  <div style="margin-top: 8px; background:#0b1020; color:#e5e7eb; border-radius:12px; padding:16px; font-size:14px; line-height:1.7; border: 1px solid #111827;">
    <div style="color:#93c5fd; font-size:12px; letter-spacing: .04em; text-transform:uppercase; margin-bottom:8px;">留言内容</div>
    <div style="white-space: pre-wrap;">{{ content }}</div>
  </div>

  <div style="margin-top: 16px; font-size:12px; color:#6b7280; display:flex; align-items:center; gap:6px;">
    <span>使用 /回复 {{ ticket }} 内容 进行回复</span>
  </div>
</div>
            """
        )

    def _reply_template(self) -> str:
        return (
            """
<div style="width: 680px; padding: 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Helvetica Neue', Arial, 'Noto Sans SC', 'Microsoft YaHei', sans-serif; background: linear-gradient(180deg,#ffffff 0%, #f8fff9 100%); color: #1f2937; border-radius: 16px; border: 1px solid #e5e7eb; box-shadow: 0 10px 30px rgba(15,23,42,0.08);">
  <div style="display:flex; align-items:center; gap:10px; margin-bottom: 12px;">
    <div style="width:10px; height:10px; background:#10b981; border-radius:50%"></div>
    <div style="font-weight:700; font-size:18px; color:#111827">留言回复 工单 {{ ticket }}</div>
  </div>

  <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; font-size: 13px; color:#374151">
    <div><span style="color:#6b7280">回复给：</span>{{ sender_name }} ({{ sender_id }})</div>
  </div>

  <div style="margin-top: 8px; background:#0b1020; color:#e5e7eb; border-radius:12px; padding:16px; font-size:14px; line-height:1.7; border: 1px solid #111827;">
    <div style="color:#86efac; font-size:12px; letter-spacing: .04em; text-transform:uppercase; margin-bottom:8px;">回复内容</div>
    <div style="white-space: pre-wrap;">{{ content }}</div>
  </div>

  <div style="margin-top: 16px; font-size:12px; color:#6b7280; display:flex; align-items:center; gap:6px;">
    <span>此回复将回送至原留言会话</span>
  </div>
</div>
            """
        )
