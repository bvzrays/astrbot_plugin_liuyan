from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import os
import json
import uuid
import asyncio
import re
import time


@register("astrbot_plugin_liuyan", "bvzrays", "留言插件：/留言 与 /回复", "1.0.0")
class LiuyanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig | None = config
        self._ticket_map: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._data_dir = self._ensure_data_dir()
        self._mapping_path = os.path.join(self._data_dir, "mappings.json")
        self._list_page: dict[str, int] = {}

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
        # 去掉指令前缀（兼容 /留言 *留言 ！留言 #留言 等，以及可选的 :： 分隔）
        message = self._strip_command_prefix(message, "留言")
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
        # 尝试获取群名（仅群聊时）
        group_name = ""
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                group_name = raw.get("group_name") or ""
        except Exception:
            pass

        # 记录映射
        img_srcs_for_store = self._extract_image_sources(event)
        async with self._lock:
            self._ticket_map[ticket] = {
                "umo": event.unified_msg_origin,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "group_id": group_id,
            "platform": platform_name,
                "status": "open",
                "created_at": int(time.time()),
                "group_name": group_name,
                "content": message,
                "has_images": True if img_srcs_for_store else False,
                "images": img_srcs_for_store[:3],
            }
        await self._save_mappings()

        # 组织转发页面（HTML 渲染为图片）
        origin_info = {
            "ticket": ticket,
            "platform": platform_name,
            "group_id": group_id or "私聊",
            "group_name": group_name,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "content": message,
        }

        # 统一发送流程，先走 AstrBot，再走协议端兜底，成功则不提示失败
        is_image = self._should_render_image()
        image_path = None
        # 提取原消息图片
        img_srcs = self._extract_image_sources(event)
        if is_image:
            try:
                image_path = await self._render_leaving_card(origin_info)
                chain = MessageChain().file_image(image_path)
                for src in img_srcs:
                    chain = chain.file_image(src)
            except Exception as e:
                logger.error(f"留言卡片渲染失败，降级为文本: {e}")
                is_image = False
                chain = self._build_text_chain_with_images(origin_info, img_srcs)
        else:
            chain = self._build_text_chain_with_images(origin_info, img_srcs)

        sent_any = False
        for umo in dest_umos:
            try:
                ok = await self.context.send_message(umo, chain)
                if ok is not False:
                    sent_any = True
                    continue
            except Exception:
                pass
            # AstrBot 发送失败，尝试协议端兜底
            try:
                if is_image and image_path:
                    await self._send_direct_aiocqhttp_image(umo, image_path)
                    # 跟随原图
                    for src in img_srcs:
                        await self._send_direct_aiocqhttp_image(umo, src)
                else:
                    before, after = self._format_liuyan_text_parts(origin_info)
                    await self._send_direct_aiocqhttp_combo(umo, before, img_srcs, after)
                sent_any = True
            except Exception as _:
                pass

        if sent_any:
            yield event.plain_result(f"留言已提交，工单号：{ticket}")
        else:
            yield event.plain_result("留言转发失败，请稍后再试或联系管理员。")

    # /回复 <工单号> <内容>
    @filter.command("回复")
    async def cmd_reply(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if not text:
            yield event.plain_result("用法：/回复 工单号 内容")
            return

        # 允许在整条消息中任意位置出现工单号（8位hex），例如：*回复 f1960660 你好
        m = re.search(r"([0-9a-fA-F]{8})", text)
        if not m:
            yield event.plain_result("工单号格式不正确，请检查后再试。")
            return
        ticket = m.group(1).lower()
        reply_text = (text[m.end():] or "").strip()
        if not reply_text:
            yield event.plain_result("回复内容不能为空。")
            return
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

        # 统一发送流程（回复）
        is_image = self._should_render_image()
        image_path = None
        img_srcs = self._extract_image_sources(event)
        if is_image:
            try:
                image_path = await self._render_reply_card(back_data)
                chain = MessageChain().file_image(image_path)
                for src in img_srcs:
                    chain = chain.file_image(src)
            except Exception as e:
                logger.error(f"回复卡片渲染失败，降级为文本: {e}")
                is_image = False
                chain = self._build_reply_chain_with_images(back_data, img_srcs)
        else:
            chain = self._build_reply_chain_with_images(back_data, img_srcs)

        sent_any = False
        try:
            ok = await self.context.send_message(dest_umo, chain)
            if ok is not False:
                sent_any = True
        except Exception:
            pass
        if not sent_any:
            try:
                if is_image and image_path:
                    await self._send_direct_aiocqhttp_image(dest_umo, image_path)
                    for src in img_srcs:
                        await self._send_direct_aiocqhttp_image(dest_umo, src)
                else:
                    before, after = self._format_reply_text_parts(back_data)
                    await self._send_direct_aiocqhttp_combo(dest_umo, before, img_srcs, after)
                sent_any = True
            except Exception as _:
                pass

        if sent_any:
            async with self._lock:
                mp = self._ticket_map.get(ticket)
                if mp:
                    mp["status"] = "closed"
                    mp["closed_at"] = int(time.time())
                    mp["last_reply"] = reply_text
            await self._save_mappings()
            yield event.plain_result("已回送给留言用户。")
        else:
            yield event.plain_result("回复发送失败，请稍后再试。")

    @filter.command("留言列表")
    async def cmd_list_tickets(self, event: AstrMessageEvent):
        dests = set(self._get_destination_umos())
        dev_ids = set((self.config.get("developer_user_ids", []) or [])) if self.config else set()
        # 允许：在任一目标会话中，或开发者本人在任意会话中
        if (event.unified_msg_origin not in dests) and (event.get_sender_id() not in dev_ids):
            yield event.plain_result("该指令仅能在留言接收会话中使用。")
            return
        async with self._lock:
            opens = [
                (k, v) for k, v in self._ticket_map.items()
                if isinstance(v, dict) and v.get("status", "open") == "open"
            ]
        if not opens:
            yield event.plain_result("暂无未处理工单。")
            return
        opens.sort(key=lambda x: x[1].get("created_at", 0), reverse=True)
        # 分页
        page_size = 5
        curr_page = self._list_page.get(event.unified_msg_origin, 1)
        total = len(opens)
        max_page = max(1, (total + page_size - 1) // page_size)
        curr_page = max(1, min(curr_page, max_page))
        start = (curr_page - 1) * page_size
        subset = opens[start:start + page_size]

        if bool(self.config.get("render_list_image", False)):
            img = await self._render_ticket_list_image(subset)
            yield event.image_result(img)
        else:
            line = "================="
            lines = [f"未处理工单 第{curr_page}/{max_page}页（每页5条）", line]
            for i, (tid, mp) in enumerate(subset, start + 1):
                gline = (mp.get('group_name','') + '（' + mp.get('group_id','') + '）') if mp.get('group_name') else (mp.get('group_id','私聊'))
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mp.get('created_at', 0)))
                preview = (mp.get('content','') or '')[:16]
                if mp.get('has_images'):
                    preview = (preview + ' [图片]') if preview else '[图片]'
                lines.append(f"{i}. {tid}")
                lines.append(f"来自：{mp.get('sender_name','')}({mp.get('sender_id','')}) | {gline} | {ts}")
                lines.append(f"摘要：{preview}")
                lines.append(line)
            yield event.plain_result("\n".join(lines))

    @filter.command("留言页码")
    async def cmd_list_page(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 1:
            yield event.plain_result("用法：/留言页码 页号")
            return
        try:
            # 兼容中文数字等简单情况
            nums = re.findall(r"\d+", text)
            page = int(nums[0]) if nums else 1
            self._list_page[event.unified_msg_origin] = max(1, page)
            yield event.plain_result(f"已切换到第 {self._list_page[event.unified_msg_origin]} 页，发送 /留言列表 查看。")
        except Exception:
            yield event.plain_result("页码格式不正确。")

    @filter.command("查看留言")
    async def cmd_view_ticket(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        m = re.search(r"([0-9a-fA-F]{8})", text)
        if not m:
            yield event.plain_result("用法：/查看留言 工单号")
            return
        ticket = m.group(1).lower()
        async with self._lock:
            mp = self._ticket_map.get(ticket)
        if not mp:
            yield event.plain_result("未找到该工单。")
            return
        gline = (mp.get('group_name','') + '（' + mp.get('group_id','') + '）') if mp.get('group_name') else (mp.get('group_id','私聊'))
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mp.get('created_at', 0)))
        line = "================="
        detail = (
            f"[留言详情] {ticket}\n{line}\n"
            f"来自：{mp.get('sender_name','')}({mp.get('sender_id','')}) | {gline} | {ts}\n{line}\n"
            f"内容：\n{mp.get('content','')}\n{line}"
        )
        chain = MessageChain().message(detail)
        for src in (mp.get('images') or [])[:3]:
            chain = chain.file_image(src)
        yield chain

    def _get_destination_umos(self) -> list[str]:
        """根据配置获取目标会话列表：
        - 使用开发者QQ/群号列表自动拼 UMO（{platform}:friend:QQ / {platform}:group:GID）；
        - 兼容单一 destination_umo；
        """
        results: list[str] = []
        if not self.config:
            return results
        platform = self._resolve_platform_name((self.config.get("platform_name", "") or "").strip())
        try:
            if bool(self.config.get("send_to_users", True)):
                user_ids = self.config.get("developer_user_ids", []) or []
                for uid in user_ids:
                    if isinstance(uid, str) and uid.strip():
                        uid_s = uid.strip()
                        # 同时兼容 friend 与 private 两种标识
                        results.append(f"{platform}:friend:{uid_s}")
                        results.append(f"{platform}:private:{uid_s}")
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

    def _resolve_platform_name(self, name: str) -> str:
        """将配置的 platform_name 进行归一化；错误或留空时回退到 aiocqhttp。
        - 允许的别名：napcat/onebot/ob11 -> aiocqhttp；default -> aiocqhttp
        - qq_official、telegram、feishu、wecom、dingtalk 按原样返回
        """
        if not name:
            return "aiocqhttp"
        lower = name.lower()
        alias_to_aiocqhttp = {"napcat", "onebot", "ob11", "aiocqhttp", "default"}
        if lower in alias_to_aiocqhttp:
            if lower == "default":
                logger.warn("platform_name=default 非平台标识，已自动回退为 aiocqhttp（Napcat）")
            return "aiocqhttp"
        allowed = {"qq_official", "telegram", "feishu", "wecom", "dingtalk"}
        if lower in allowed:
            return lower
        # 未知值时回退
        logger.warn(f"未知的平台标识 '{name}'，已回退为 aiocqhttp")
        return "aiocqhttp"

    def _normalize_ticket(self, token: str) -> str | None:
        if not token:
            return None
        m = re.search(r"([0-9a-fA-F]{8})", token)
        return m.group(1).lower() if m else None

    def _strip_command_prefix(self, text: str, cmd: str) -> str:
        try:
            if not text:
                return ""
            # 允许形如：/留言 xxx, *留言 xxx, ！留言 xxx, #留言 xxx, 留言: xxx, 留言 xxx
            pattern = rf"^[\\s*/#*!！]?{cmd}[\\s:：]*"
            return re.sub(pattern, "", text, count=1).strip()
        except Exception:
            return text

    async def _send_direct_aiocqhttp(self, umo: str, text: str):
        """直接通过 aiocqhttp 协议端 API 发送文本兜底。
        仅在 context.send_message 失败时调用。
        支持的 UMO：aiocqhttp:group:<gid> / aiocqhttp:friend:<qq> / aiocqhttp:private:<qq>
        """
        try:
            parts = (umo or "").split(":", 2)
            if len(parts) != 3:
                return
            platform, msg_type, sid = parts
            if platform != "aiocqhttp":
                return
            platform_inst = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform_inst:
                return
            client = platform_inst.get_client()
            if msg_type == "group":
                await client.api.call_action('send_group_msg', group_id=int(sid), message=text)
            elif msg_type in {"friend", "private"}:
                await client.api.call_action('send_private_msg', user_id=int(sid), message=text)
        except Exception as e:
            logger.error(f"直接调用 aiocqhttp 发送失败: {e}")

    async def _send_direct_aiocqhttp_image(self, umo: str, image_path: str):
        """通过 aiocqhttp 直接发送图片（CQ 码）。"""
        try:
            from pathlib import Path
            parts = (umo or "").split(":", 2)
            if len(parts) != 3:
                return
            platform, msg_type, sid = parts
            if platform != "aiocqhttp":
                return
            # 支持 http/https 与本地文件
            if isinstance(image_path, str) and (image_path.startswith("http://") or image_path.startswith("https://")):
                cq = f"[CQ:image,file={image_path}]"
            else:
                uri = Path(image_path).resolve().as_uri()
                cq = f"[CQ:image,file={uri}]"
            platform_inst = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform_inst:
                return
            client = platform_inst.get_client()
            if msg_type == "group":
                await client.api.call_action('send_group_msg', group_id=int(sid), message=cq)
            elif msg_type in {"friend", "private"}:
                await client.api.call_action('send_private_msg', user_id=int(sid), message=cq)
        except Exception as e:
            logger.error(f"直接调用 aiocqhttp 发送图片失败: {e}")

    async def _send_direct_aiocqhttp_combo(self, umo: str, text_before: str, image_sources: list[str], text_after: str):
        """通过 aiocqhttp 一次性发送 文本 + 多图片 + 文本。"""
        try:
            from pathlib import Path
            parts = (umo or "").split(":", 2)
            if len(parts) != 3:
                return
            platform, msg_type, sid = parts
            if platform != "aiocqhttp":
                return
            pieces = [text_before]
            for src in (image_sources or []):
                if isinstance(src, str) and (src.startswith("http://") or src.startswith("https://")):
                    pieces.append(f"[CQ:image,file={src}]")
                else:
                    uri = Path(src).resolve().as_uri()
                    pieces.append(f"[CQ:image,file={uri}]")
            if text_after:
                pieces.append("\n" + text_after)
            msg = "".join(pieces)

            platform_inst = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform_inst:
                return
            client = platform_inst.get_client()
            if msg_type == "group":
                await client.api.call_action('send_group_msg', group_id=int(sid), message=msg)
            elif msg_type in {"friend", "private"}:
                await client.api.call_action('send_private_msg', user_id=int(sid), message=msg)
        except Exception as e:
            logger.error(f"直接调用 aiocqhttp 组合发送失败: {e}")

    def _extract_image_sources(self, event: AstrMessageEvent):
        try:
            raw = event.message_obj.raw_message
            arr = raw.get('message') if isinstance(raw, dict) else None
            urls = []
            if isinstance(arr, list):
                for seg in arr:
                    if isinstance(seg, dict) and seg.get('type') == 'image':
                        data = seg.get('data') or {}
                        u = data.get('url') or data.get('file')
                        if isinstance(u, str) and u:
                            urls.append(u)
            return urls
        except Exception:
            return []

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
        line = "================="
        return (
            f"[留言工单] {data.get('ticket','')}\n"
            f"{line}\n"
            f"来源群：{(data.get('group_name') + '（' + data.get('group_id') + '）') if data.get('group_name') else data.get('group_id','私聊')}\n"
            f"来源用户：{data.get('sender_name','')} ({data.get('sender_id','')})\n"
            f"{line}\n"
            f"内容：\n{data.get('content','')}\n"
            f"{line}\n"
            f"使用 /回复 {data.get('ticket','')} 内容 进行回复"
        )

    def _format_reply_text(self, data: dict) -> str:
        line = "================="
        return (
            f"[留言回复] 工单 {data.get('ticket','')}\n"
            f"{line}\n"
            f"回复给：{data.get('sender_name','')} ({data.get('sender_id','')})\n"
            f"{line}\n"
            f"内容：\n{data.get('content','')}"
        )

    def _format_reply_text_parts(self, data: dict) -> tuple[str, str]:
        line = "================="
        before = (
            f"[留言回复] 工单 {data.get('ticket','')}\n"
            f"{line}\n"
            f"回复给：{data.get('sender_name','')} ({data.get('sender_id','')})\n"
            f"{line}\n"
            f"内容：\n{data.get('content','')}\n"
        )
        after = ""
        return before, after

    def _build_reply_chain_with_images(self, data: dict, image_sources: list[str]) -> MessageChain:
        before, after = self._format_reply_text_parts(data)
        chain = MessageChain().message(before)
        for src in image_sources:
            chain = chain.file_image(src)
        if after:
            chain = chain.message("\n" + after)
        return chain

    def _format_liuyan_text_parts(self, data: dict) -> tuple[str, str]:
        line = "================="
        before = (
            f"[留言工单] {data.get('ticket','')}\n"
            f"{line}\n"
            f"来源群：{(data.get('group_name') + '（' + data.get('group_id') + '）') if data.get('group_name') else data.get('group_id','私聊')}\n"
            f"来源用户：{data.get('sender_name','')} ({data.get('sender_id','')})\n"
            f"{line}\n"
            f"内容：\n{data.get('content','')}\n"
        )
        after = (
            f"{line}\n"
            f"使用 /回复 {data.get('ticket','')} 内容 进行回复"
        )
        return before, after

    def _build_text_chain_with_images(self, data: dict, image_sources: list[str]) -> MessageChain:
        before, after = self._format_liuyan_text_parts(data)
        chain = MessageChain().message(before)
        for src in image_sources:
            chain = chain.file_image(src)
        chain = chain.message("\n" + after)
        return chain

    async def _render_leaving_card(self, data: dict) -> str:
        """将留言数据渲染为图片并返回本地路径。"""
        tmpl = self._liuyan_template()
        path = await self.html_render(tmpl, data, return_url=True, options={
            "type": "png",
            "omit_background": True,
            "full_page": True
        })
        return path

    async def _render_reply_card(self, data: dict) -> str:
        """将回复数据渲染为图片并返回本地路径。"""
        tmpl = self._reply_template()
        path = await self.html_render(tmpl, data, return_url=True, options={
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
    <div><span style="color:#6b7280">来源群：</span>{% if group_name %}{{ group_name }}（{{ group_id }}）{% else %}{{ group_id }}{% endif %}</div>
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

    async def _render_ticket_list_image(self, items: list[tuple[str, dict]]) -> str:
        """渲染未处理工单列表为图片。"""
        tmpl = self._list_template()
        # 组装显示数据
        data_items = []
        for tid, mp in items:
            data_items.append({
                "title": f"工单 {tid}",
                "version": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mp.get("created_at", 0))),
                "behavior": f"来自 {mp.get('sender_name','')}({mp.get('sender_id','')})",
                "desc": f"会话：{(mp.get('group_name','') + '（' + mp.get('group_id','') + '）') if mp.get('group_name') else (mp.get('group_id','私聊'))}",
            })
        path = await self.html_render(tmpl, {"items": data_items}, return_url=False, options={
            "type": "png",
            "omit_background": False,
            "full_page": True
        })
        return path

    def _list_template(self) -> str:
        return (
            """
<!DOCTYPE html>
<html>
<head>
  <meta charset='UTF-8'>
  <title>未处理工单</title>
  <style>
    body { font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px; }
    .plugin-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }
    .plugin-card { background-color: #2d2d2d; color: #fff; border-radius: 8px; padding: 20px; display: flex; flex-direction: column; height: 100%; }
    .plugin-title { font-size: 18px; font-weight: bold; margin-bottom: 5px; }
    .plugin-version { display:flex; align-items:center; margin-bottom: 5px; font-size: 14px; color: #aaa; }
    .plugin-behavior { display:flex; align-items:center; margin-bottom: 10px; font-size: 14px; color: #aaa; }
    .plugin-description { font-size: 14px; line-height: 1.5; color: #ccc; }
    .plugin-icon { margin-left: auto; display:flex; align-items:center; }
    .plugin-icon i { margin-left: 10px; font-size: 20px; }
    .sep { margin: 10px 0; height:1px; background:#555; }
  </style>
  </head>
<body>
  <div class='plugin-grid'>
    {% for it in items %}
    <div class='plugin-card'>
      <div class='plugin-header' style='display:flex; align-items:center;'>
        <div class='plugin-title'>{{ it.title }}</div>
        <div class='plugin-icon'><i>🔄</i><i>⋮</i></div>
      </div>
      <div class='plugin-version'>{{ it.version }}</div>
      <div class='plugin-behavior'>{{ it.behavior }}</div>
      <div class='sep'></div>
      <div class='plugin-description'>{{ it.desc }}</div>
    </div>
    {% endfor %}
  </div>
</body>
</html>
            """
        )
