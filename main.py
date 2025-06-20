from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig
from astrbot.core.utils.session_waiter import session_waiter, SessionController
import asyncio
import json

@register("textadventure", "LumineStory", "一个动态文字冒险小游戏", "1.0.0")
class TextAdventurePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 从配置中获取默认主题，如果未配置则使用“奇幻世界”
        self.default_adventure_theme = self.config.get("default_adventure_theme", "奇幻世界")
        logger.info(f"TextAdventurePlugin initialized with default theme: {self.default_adventure_theme}")

    @filter.command("开始冒险")
    async def start_adventure(self, event: AstrMessageEvent, theme: str = None):
        """
        开始一场动态文字冒险游戏。
        用法: /开始冒险 [主题]
        例如: /开始冒险 在一个赛博朋克城市
        """
        user_id = event.get_sender_id()
        game_theme = theme if theme else self.default_adventure_theme
        
        # 1. 发送免责声明和游玩方式
        disclaimer_and_instructions = (
            "📜 动态文字冒险 - 游戏须知 📜\n\n"
            "免责声明：\n"
            "本文字冒险游戏由AI驱动，故事内容由大语言模型实时生成。请注意，AI生成的内容可能包含虚构、非现实或不符合逻辑的情节。游戏旨在提供娱乐，请勿将内容与现实混淆。\n\n"
            "💡 游戏玩法：\n"
            "1. 游戏主持人(DM)会为你描述当前场景并提供行动选项或提示你自由输入行动。\n"
            "2. 你可以直接输入你的行动（例如：“向左走”、“检查宝箱”、“和守卫对话”）。\n"
            "3. 行动可以非常具体，也可以尝试一些创新的想法！\n"
            "4. DM会根据你的行动推进故事。\n"
            "5. 每回合你有300秒（5分钟）的时间输入行动。如果超时未输入，游戏将自动结束。\n"
            "你可以随时发送 /结束冒险 来退出游戏。\n\n"
            "现在，冒险即将开始... 祝你旅途愉快！"
        )
        yield event.plain_result(disclaimer_and_instructions)
        
        # 初始化游戏状态，存储在内存中以便会话使用
        game_state = {
            "theme": game_theme,
            "llm_conversation_context": [], # 存储与LLM对话的历史，OpenAI格式
        }

        # 构建LLM的系统提示词，将其设置为游戏主持人 (Game Master)
        system_prompt = (
            f"你是一位经验丰富的文字冒险游戏主持人 (Game Master)。"
            f"你将根据玩家的行动，在'{game_theme}'主题下实时生成独特的故事情节和挑战。"
            f"你的目标是创造一个引人入胜、逻辑连贯且充满未知的故事。"
            f"每次回复请包含以下内容：\n"
            f"1. 对当前场景的详细描述。\n"
            f"2. 玩家的当前状态或遇到的情况。\n"
            f"3. 几个可能的行动选项 (例如：A. 探索 B. 调查 C. 与NPC交流)，或者明确告知玩家可以自由输入行动。\n"
            f"确保故事风格一致，并避免重复。保持简短，每次回复约200字左右。"
        )
        game_state["llm_conversation_context"].append({"role": "system", "content": system_prompt})
        game_state["llm_conversation_context"].append({"role": "user", "content": f"开始一场关于{game_theme}的冒险。"})

        # 获取当前使用的LLM提供商实例
        llm_provider = self.context.get_using_provider()
        if not llm_provider:
            yield event.plain_result("抱歉，当前没有可用的LLM服务来开始冒险。请联系管理员启用LLM服务。")
            return

        try:
            # 首次调用LLM，生成开场场景
            llm_response = await llm_provider.text_chat(
                prompt="", # 初始提示词由 contexts 中的 system_prompt 和第一个 user message 提供
                session_id=event.get_session_id(),
                contexts=game_state["llm_conversation_context"],
                image_urls=[],
                func_tool=None,
                system_prompt="", # System prompt 已包含在 contexts 中
            )
            initial_story_text = llm_response.completion_text
            game_state["llm_conversation_context"].append({"role": "assistant", "content": initial_story_text})
            
            yield event.plain_result(f"\n{initial_story_text}") # 在免责声明后发送开场故事

            @session_waiter(timeout=300, record_history_chains=False) # 设置每回合5分钟超时
            async def adventure_waiter(controller: SessionController, event: AstrMessageEvent):
                player_action = event.message_str.strip() # 获取玩家输入的行动
                if not player_action:
                    yield event.plain_result("你什么也没做。请告诉我你的行动。")
                    controller.keep(timeout=300, reset_timeout=True) # 保持会话并重置超时
                    return

                # 将玩家的行动添加到LLM对话历史中
                game_state["llm_conversation_context"].append({"role": "user", "content": player_action})

                # 再次调用LLM，根据玩家行动生成后续故事
                try:
                    llm_response = await llm_provider.text_chat(
                        prompt="", # 玩家行动现在已在 contexts 中
                        session_id=event.get_session_id(),
                        contexts=game_state["llm_conversation_context"],
                        image_urls=[],
                        func_tool=None,
                        system_prompt="",
                    )
                    story_text = llm_response.completion_text
                    game_state["llm_conversation_context"].append({"role": "assistant", "content": story_text})

                    yield event.plain_result(story_text)
                    controller.keep(timeout=300, reset_timeout=True) # 重置超时时间，等待下一回合玩家输入

                except Exception as e:
                    logger.error(f"LLM调用失败: {e}")
                    yield event.plain_result("抱歉，冒险过程中LLM服务出现问题，游戏暂时无法继续。请尝试 /结束冒险 并重新开始。")
                    controller.stop() # LLM调用失败时结束会话

            try:
                await adventure_waiter(event) # 启动会话等待器
            except asyncio.TimeoutError: # 捕获超时错误
                yield event.plain_result("⏱️ 冒险超时了！你的角色陷入了沉睡，游戏已自动结束。你可以使用 /开始冒险 重新开始新的冒险。")
            except Exception as e:
                logger.error(f"冒险游戏发生未知错误: {e}")
                yield event.plain_result("冒险过程中发生未知错误，游戏已结束。")
            finally:
                event.stop_event() # 确保事件在游戏会话结束后停止传播

        except Exception as e:
            logger.error(f"开始冒险时LLM调用失败: {e}")
            yield event.plain_result("抱歉，无法开始冒险游戏，LLM服务出现问题。请确保LLM服务已正确配置并可用。")

    @filter.command("结束冒险")
    async def end_adventure(self, event: AstrMessageEvent):
        """
        结束当前的文字冒险游戏。
        """
        yield event.plain_result("✅ 冒险已结束。感谢您的参与！")
        event.stop_event() # 停止事件传播

    @filter.command("冒险帮助")
    async def cmd_adventure_help(self, event: AstrMessageEvent):
        """
        显示动态文字冒险插件的所有可用命令及其说明。
        """
        help_message = (
            "📜 动态文字冒险帮助 📜\n\n"
            "欢迎来到文字冒险的世界，你的每一个选择都将塑造独特的故事！\n\n"
            "🎲 游玩指令:\n"
            "  - /开始冒险 [主题/初始设定]: 开始一场新的冒险。\n"
            "    - 例如: /开始冒险 在一个赛博朋克城市\n"
            "    - 如果不指定主题，将使用默认主题。\n"
            "  - /结束冒险: 随时结束当前的冒险游戏。\n\n"
            "💡 游戏玩法:\n"
            "  - 游戏开始后，AI (游戏主持人) 会生成开场场景并提供行动选项，或提示你自由输入行动。\n"
            "  - 输入你的行动（例如“调查巷子里的声音”，“尝试进入酒吧”），行动可以非常具体和创新。\n"
            "  - AI 将根据你的行动，实时生成后续故事、新场景、遇到的角色、以及随之而来的挑战和后果。\n"
            "  - 游戏没有固定结局，完全开放，玩家的目标是探索、生存或达成自己的目标。\n\n"
            "⏱️ 超时说明:\n"
            "  - 每回合你有300秒（5分钟）的时间输入行动。\n"
            "  - 如果超时未输入，游戏将自动结束，你的角色将陷入沉睡。你可以使用 /开始冒险 重新开始。\n\n"
            "祝你旅途愉快！"
        )
        yield event.plain_result(help_message)

    async def terminate(self):
        """插件终止时调用，用于清理资源。"""
        logger.info("TextAdventurePlugin terminated.")