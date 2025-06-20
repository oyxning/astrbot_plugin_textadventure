# main.py
import asyncio
from typing import Dict, List

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter


@register("textadventure", "YourName", "一个将故事文字渲染为图片的动态冒险小游戏", "1.4.1")
class TextAdventurePlugin(Star):
    """
    一个由LLM驱动的图文动态文字冒险游戏插件。
    此版本将AI生成的故事文字直接渲染为图片进行展示，并包含健壮的会话管理。
    核心参数（如超时时间、系统提示词）均可通过配置文件修改。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 从配置文件读取设置，并提供默认值
        self.default_adventure_theme = self.config.get("default_adventure_theme", "奇幻世界")
        self.session_timeout = self.config.get("session_timeout", 300)
        self.system_prompt_template = self.config.get(
            "system_prompt_template",
            "你是一位经验丰富的文字冒险游戏主持人(Game Master)。你将在一个'{game_theme}'主题下，根据玩家的行动实时生成独特且逻辑连贯的故事情节。"
            "你的目标是创造一个引人入胜、充满未知的故事。你的回复应包含：\n"
            "1. 对场景的生动描述。\n"
            "2. 玩家的当前状况。\n"
            "3. 引导玩家思考下一步行动，可以给出几个选项（例如：A. ... B. ...），或直接鼓励玩家自由探索。\n"
            "请确保故事风格一致，并避免重复。保持回复在200-300字左右，以便渲染为图片。"
        )
        
        # 增加日志记录，方便调试，确认配置是否加载成功
        logger.info("--- TextAdventurePlugin 配置加载 ---")
        logger.info(f"默认主题: {self.default_adventure_theme}")
        logger.info(f"会话超时: {self.session_timeout} 秒")
        logger.info(f"系统提示词模板: {self.system_prompt_template[:70]}...")
        logger.info("------------------------------------")

        # 存储活跃的游戏会话。键: sender_id, 值: SessionController 实例
        self.active_game_sessions: Dict[str, SessionController] = {}

    @filter.command("开始冒险")
    async def start_adventure(self, event: AstrMessageEvent, theme: str = ""):
        """
        开始一场动态文字冒险游戏。
        用法: /开始冒险 [可选的主题]
        例如: /开始冒险 在一个赛博朋克城市
        """
        user_id = event.get_sender_id()
        game_theme = theme.strip() if theme else self.default_adventure_theme

        if user_id in self.active_game_sessions:
            yield event.plain_result(f"您已经有一个正在进行的冒险了！\n- 如需继续，请直接输入您的行动。\n- 如需结束，请发送 /结束冒险 或 /强制结束冒险。\n(玩家ID: {user_id})")
            return

        # 发送免责声明和游玩方式
        disclaimer_and_instructions = (
            "📜 **动态图文冒险 - 游戏须知** 📜\n\n"
            "**免责声明**：\n"
            "本游戏由AI驱动，故事内容由大语言模型实时生成。为了提升阅读体验，故事文字将被渲染成图片发送。\n\n"
            "**💡 游戏玩法**：\n"
            f"1. 游戏主持人(DM)会描述场景，你可以自由输入行动（如：“向左走”、“检查宝箱”）。\n"
            f"2. DM会根据你的行动推进故事，每回合有 **{self.session_timeout}秒** 的行动时间，超时游戏将自动结束。\n"
            "3. 你可以随时发送 `/结束冒险` 或 `/强制结束冒险` 来退出游戏。\n\n"
            "现在，冒险即将开始... 祝你旅途愉快！"
        )
        yield event.plain_result(disclaimer_and_instructions)

        # 初始化游戏状态
        game_state = {
            "theme": game_theme,
            "llm_conversation_context": [],  # OpenAI格式的对话历史
        }

        # 使用配置文件中的模板构建系统提示词
        try:
            system_prompt = self.system_prompt_template.format(game_theme=game_theme)
        except KeyError:
            logger.error("系统提示词模板格式错误！缺少 `{game_theme}` 占位符。将使用默认模板。")
            system_prompt = f"你是一位经验丰富的文字冒险游戏主持人(Game Master)。你将在一个'{game_theme}'主题下，根据玩家的行动实时生成独特且逻辑连贯的故事情节。"
        
        game_state["llm_conversation_context"].append({"role": "system", "content": system_prompt})
        game_state["llm_conversation_context"].append({"role": "user", "content": f"故事开始了，我的第一个场景是什么？"})

        llm_provider = self.context.get_using_provider()
        if not llm_provider:
            yield event.plain_result(f"抱歉，当前没有可用的LLM服务来开始冒险。请联系管理员。(玩家ID: {user_id})")
            return

        # 首次调用LLM生成开场
        try:
            yield event.plain_result("正在构筑您的冒险世界，请稍候...")
            llm_response: LLMResponse = await llm_provider.text_chat(
                prompt="",
                session_id=event.get_session_id(),
                contexts=game_state["llm_conversation_context"],
            )
            story_text = llm_response.completion_text
            game_state["llm_conversation_context"].append({"role": "assistant", "content": story_text})

            # 将返回的文字渲染为图片
            try:
                image_path = await self.text_to_image(story_text, return_url=False)
                message_chain = MessageChain([
                    Comp.Image.from_file_system(image_path),
                    Comp.Plain(f"\n\n**[提示: 请直接输入你的行动]** (玩家ID: {user_id})")
                ])
                yield message_chain
            except Exception as img_e:
                logger.error(f"文字转图片失败: {img_e}，将发送纯文本。")
                yield event.plain_result(f"{story_text}\n\n[文字渲染图片失败]\n**[提示: 请直接输入你的行动]** (玩家ID: {user_id})")

        except Exception as e:
            logger.error(f"开始冒险时LLM调用失败: {e}")
            yield event.plain_result(f"抱歉，无法开始冒险，LLM服务出现问题。(玩家ID: {user_id})")
            return

        # 定义会话等待器，使用配置文件中的超时时间
        @session_waiter(timeout=self.session_timeout, record_history_chains=False)
        async def adventure_waiter(controller: SessionController, event: AstrMessageEvent):
            user_id = event.get_sender_id()

            if user_id not in self.active_game_sessions:
                logger.info(f"会话 for {user_id} 已被强制终止，停止处理新的行动。")
                controller.stop()
                return

            self.active_game_sessions[user_id] = controller

            player_action = event.message_str.strip()
            if not player_action:
                await event.send(MessageChain([Comp.Plain(f"你静静地站着，什么也没做。要继续冒险，请输入你的行动。\n(玩家ID: {user_id})")]))
                controller.keep(timeout=self.session_timeout, reset_timeout=True)
                return
            
            await event.send(MessageChain([Comp.Plain("AI正在构思下一幕...请稍等片刻...")]))
            game_state["llm_conversation_context"].append({"role": "user", "content": player_action})

            try:
                llm_response = await llm_provider.text_chat(
                    prompt="",
                    session_id=event.get_session_id(),
                    contexts=game_state["llm_conversation_context"],
                )
                story_text = llm_response.completion_text
                game_state["llm_conversation_context"].append({"role": "assistant", "content": story_text})

                if user_id not in self.active_game_sessions:
                    logger.info(f"会话 for {user_id} 在LLM响应期间被强制终止，不再发送消息。")
                    controller.stop()
                    return

                # 将返回的文字渲染为图片
                try:
                    image_path = await self.text_to_image(story_text, return_url=False)
                    message_chain = MessageChain([
                        Comp.Image.from_file_system(image_path),
                        Comp.Plain(f"\n\n**[提示: 请直接输入你的行动]** (玩家ID: {user_id})")
                    ])
                    await event.send(message_chain)
                except Exception as img_e:
                    logger.error(f"文字转图片失败: {img_e}，将发送纯文本。")
                    await event.send(event.plain_result(f"{story_text}\n\n[文字渲染图片失败]\n**[提示: 请直接输入你的行动]** (玩家ID: {user_id})"))

                controller.keep(timeout=self.session_timeout, reset_timeout=True)

            except Exception as e:
                logger.error(f"冒险过程中LLM调用失败: {e}")
                await event.send(MessageChain([Comp.Plain(f"抱歉，AI的思绪似乎被卡住了，游戏暂时无法继续。请尝试 /强制结束冒险 并重新开始。\n(玩家ID: {user_id})")]))
                if user_id in self.active_game_sessions:
                    del self.active_game_sessions[user_id]
                controller.stop()

        # 启动会话
        try:
            self.active_game_sessions[user_id] = None # 先占个位置
            await adventure_waiter(event)
        except asyncio.TimeoutError:
            yield event.plain_result(f"⏱️ **冒险超时！**\n你的角色在原地陷入了沉睡，游戏已自动结束。使用 /开始冒险 来唤醒他/她，或开始新的冒险。\n(玩家ID: {user_id})")
        except Exception as e:
            logger.error(f"冒险游戏发生未知错误: {e}")
            yield event.plain_result(f"冒险过程中发生未知错误，游戏已结束。\n(玩家ID: {user_id})")
        finally:
            if user_id in self.active_game_sessions:
                del self.active_game_sessions[user_id]
                logger.info(f"用户 {user_id} 的游戏会话已清理并从 active_game_sessions 中移除。")
            event.stop_event()

    @filter.command("结束冒险")
    async def end_adventure(self, event: AstrMessageEvent):
        """
        优雅地结束当前的文字冒险游戏。
        """
        user_id = event.get_sender_id()
        if user_id in self.active_game_sessions:
            controller = self.active_game_sessions[user_id]
            if controller:
                controller.stop() # 发出停止信号
            yield event.plain_result(
                f"✅ **冒险结束指令已发出**。\n"
                f"如果AI正在响应，将在本回合结束后终止。如果游戏长时间无响应，请使用 /强制结束冒险。\n"
                f"(玩家ID: {user_id})"
            )
        else:
            yield event.plain_result(f"您当前没有正在进行的冒险。\n(玩家ID: {user_id})")
        event.stop_event()

    @filter.command("强制结束冒险")
    async def force_end_adventure(self, event: AstrMessageEvent):
        """
        立即强制结束当前的文字冒险游戏。
        """
        user_id = event.get_sender_id()
        if user_id in self.active_game_sessions:
            controller = self.active_game_sessions.pop(user_id, None) # 立即从活跃会话中移除
            if controller:
                controller.stop() # 同时发出停止信号以触发清理
            logger.info(f"用户 {user_id} 的游戏会话已被强制终止。")
            yield event.plain_result(
                f"💥 **冒险已强制终止！**\n"
                f"您可以随时通过 /开始冒险 开启新的旅程。\n"
                f"(玩家ID: {user_id})"
            )
        else:
            yield event.plain_result(f"您当前没有正在进行的冒险。\n(玩家ID: {user_id})")
        event.stop_event()


    @filter.command("admin end")
    async def cmd_admin_end_all_games(self, event: AstrMessageEvent):
        """
        管理员命令：立即强制结束所有在线的文字冒险游戏。
        """
        if not event.is_admin():
            yield event.plain_result("❌ 权限不足，只有管理员可操作此命令。")
            event.stop_event()
            return
        
        if not self.active_game_sessions:
            yield event.plain_result("当前没有活跃的文字冒险游戏。")
            event.stop_event()
            return

        stopped_count = len(self.active_game_sessions)
        for user_id, controller in list(self.active_game_sessions.items()):
            if controller:
                controller.stop()
            del self.active_game_sessions[user_id] # 强制移除
        
        yield event.plain_result(
            f"✅ **管理员操作完成**。\n"
            f"已强制终止所有 {stopped_count} 个活跃的文字冒险游戏。"
        )
        logger.info(f"管理员 {event.get_sender_id()} 强制结束了所有 {stopped_count} 个游戏。")
        event.stop_event()

    @filter.command("冒险帮助")
    async def cmd_adventure_help(self, event: AstrMessageEvent):
        """
        显示动态文字冒险插件的所有可用命令。
        """
        help_message = (
            "📜 **动态图文冒险 - 帮助手册** 📜\n\n"
            "欢迎来到由AI驱动的图文冒险世界！在此版本中，故事文字会被渲染成图片发送。\n\n"
            "**基本指令**:\n"
            "  - `/开始冒险 [可选主题]`：开始一场新冒险。若不指定主题，则使用默认主题。\n"
            "    *例如: /开始冒险 探索一座被遗忘的深海城市*\n"
            "  - `/结束冒险`：**优雅结束**当前游戏。会在当前AI回合结束后停止。\n"
            "  - `/强制结束冒险`：**立即结束**当前游戏。当游戏卡住时使用此指令。\n\n"
            "**管理员指令**:\n"
            "  - `/admin end`：强制结束所有正在进行的游戏。\n\n"
            "**💡 游戏玩法**:\n"
            "  - 游戏开始后，AI游戏主持人会发送一张包含故事场景的图片。\n"
            "  - 您只需直接输入您的行动（例如“调查那个奇怪的符号”，“和酒馆老板搭话”），AI便会根据您的输入推进故事，并发送新的场景图片。\n"
        )
        yield event.plain_result(help_message)
        event.stop_event()

    async def terminate(self):
        """插件终止时调用，用于清理所有活跃的游戏会话。"""
        logger.info("正在终止 TextAdventurePlugin 并清理所有活跃的游戏会话...")
        if self.active_game_sessions:
            for user_id, controller in list(self.active_game_sessions.items()):
                if controller:
                    controller.stop()
            self.active_game_sessions.clear()
            logger.info("所有活跃的游戏会话已被终止。")
        logger.info("TextAdventurePlugin terminated。")
