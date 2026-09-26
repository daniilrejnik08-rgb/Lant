import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timezone, timedelta
import io
import logging
import os
import re
import json
from typing import NamedTuple, Optional
from collections import defaultdict, deque
import aiohttp

try:
    import pytesseract
    from PIL import Image
    HAS_OCR = True
except ImportError:
    pytesseract = None
    Image = None
    HAS_OCR = False
    print("[ScamScanner] pytesseract/Pillow не установлены — OCR картинок отключён")

# ====================== КОНФИГ ======================
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_TOKEN_HERE")
GUILD_ID = 1500474194771574966
CATEGORY_TICKETS_ID = 1505876649554612294
ROLE_STAFF_ID = 1500474195211980977
INFO_CHANNEL_ID = 1500861419363500032
LOG_CHANNEL_ID = 1506392661454487822
APPLICATIONS_LOG_CHANNEL_ID = 1513721222578311279
AUDIT_CHANNEL_ID = 1506392661454487822

BANNER_URL = "https://media.discordapp.net/attachments/1500474195623149764/1505932228461334609/zxc.png?ex=6a0c6c2e&is=6a0b1aae&hm=ece545622b439fef92262fb1d0b10bc2f5e84785a2613cdc7feae759e772f325&=&format=webp&quality=lossless"
RUBANNER_URL = "https://media.discordapp.net/attachments/1500475963585073264/1512470605172179024/rules.jpg?ex=6a243584&is=6a22e404&hm=1b7c6e512e721bb1cf09568a00af6825bd27f55cc8492cbc5c65c5095110f16a&=&format=webp"
SEPARATOR_URL = "https://media.discordapp.net/attachments/1500474195623149764/1505936040622424234/zxc2.png?ex=6a0c6fbb&is=6a0b1e3b&hm=0bdbd5aaee6b20782b84216208e4c6096d3fcbbe00edefc78e4b4f91080fd464&=&format=webp&quality=lossless"

SCAM_NOTIFY_CHANNEL_ID = "1512557043477647550"
SCAM_TESSDATA_PATH = os.getenv("TESSDATA_PREFIX", "/usr/share/tesseract-ocr/4.00/tessdata")
SCAM_LANGUAGE = "eng+rus"
SCAM_PHRASES = [
    "bonus", "usdt", "withdraw", "congratulations", "promo code",
    "activate code for bonus", "reward received", "receive usdt",
    "бонус", "выводить", "казино", "регистрац",
    "раздаёт", "каждому", "выигрыш",
    "честь", "пользователю", "играть",
]
SCAM_LINK_BLACKLIST = [
    "t.me/", "telegram.me/", "bit.ly/", "cutt.ly/", "clck.ru/",
    "goo.gl/", "tinyurl.com/",
]
SCAM_MUTE_THRESHOLD = 3
SCAM_MUTE_MINUTES = 60

PUNISHED_ROLE_IDS = []

APPLICATION_ROLE_MAP = {
    "CS:GO": None,
    "CS2": None,
    "Discord Staff": None,
}

TICKET_INACTIVE_HOURS = 48
TICKET_USER_REMIND_HOURS = 12
TICKET_MAX_PER_USER = 1
APPLICATION_COOLDOWN_DAYS = 7
APPLICATION_STALE_HOURS = 72

FLOOD_SAME_COUNT = 3
FLOOD_WINDOW_SECONDS = 60
FLOOD_MUTE_MINUTES = 10

AUTO_REPLIES = {
    "как зайти": f"Инструкция по подключению находится в <#{INFO_CHANNEL_ID}>. Если не помогло — откройте тикет.",
    "ошибка подключения": f"Попробуйте: очистить DNS, отключить VPN/прокси, перезапустить игру. Подробнее: <#{INFO_CHANNEL_ID}>",
    "не могу зайти": f"Смотрите канал <#{INFO_CHANNEL_ID}>. Если ничего не помогло — откройте тикет с скриншотом ошибки.",
    "сайт не работает": "Очистите кэш браузера или попробуйте режим Инкогнито. Если ошибка остаётся — откройте тикет со скриншотом.",
    "как подать апелляцию": "Выберите в меню тикетов пункт «Апелляция блокировки».",
    "баг": "Нашли баг? Откройте тикет категории «Репорт-баг» и опишите шаги воспроизведения + скрин/видео.",
}

DATA_FILE = "bot_data.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ZXCBot")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIMENSION = 2000


class DataStore:
    def __init__(self, path: str):
        self.path = path
        self.data = {
            "application_cooldowns": {},
            "scam_strikes": {},
            "scam_phrases": list(SCAM_PHRASES),
            "stats": {
                "tickets_opened": 0,
                "tickets_closed": 0,
                "applications_accepted": 0,
                "applications_declined": 0,
                "scam_caught": 0,
            },
            "ticket_claims": {},
            "ticket_last_activity": {},
            "ticket_owners": {},
            "ticket_opened_at": {},
            "blacklist": [],
        }
        self.load()
        self.flood: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
            except Exception as e:
                log.warning(f"Не удалось загрузить data: {e}")

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Не удалось сохранить data: {e}")

    def get_phrases(self) -> list:
        return self.data.get("scam_phrases", list(SCAM_PHRASES))

    def add_phrase(self, phrase: str) -> bool:
        p = phrase.lower().strip()
        if p and p not in self.data["scam_phrases"]:
            self.data["scam_phrases"].append(p)
            self.save()
            return True
        return False

    def remove_phrase(self, phrase: str) -> bool:
        p = phrase.lower().strip()
        if p in self.data["scam_phrases"]:
            self.data["scam_phrases"].remove(p)
            self.save()
            return True
        return False

    def is_blacklisted(self, user_id: int) -> bool:
        return str(user_id) in self.data.get("blacklist", [])

    def blacklist_add(self, user_id: int) -> bool:
        uid = str(user_id)
        if uid not in self.data["blacklist"]:
            self.data["blacklist"].append(uid)
            self.save()
            return True
        return False

    def blacklist_remove(self, user_id: int) -> bool:
        uid = str(user_id)
        if uid in self.data["blacklist"]:
            self.data["blacklist"].remove(uid)
            self.save()
            return True
        return False

    def count_open_tickets(self, user_id: int) -> int:
        return sum(1 for uid in self.data.get("ticket_owners", {}).values() if int(uid) == user_id)


store = DataStore(DATA_FILE)


class ImageRef(NamedTuple):
    url: str
    file_name: str


class ScamImageScanner:
    def __init__(self, bot: discord.Client) -> None:
        self.notify_channel_id = SCAM_NOTIFY_CHANNEL_ID
        self._tess_config = f"--psm 6 -l {SCAM_LANGUAGE}"
        self._semaphore = asyncio.Semaphore(2)
        self.bot = bot
        self.ready = False
        if not HAS_OCR:
            print("[ScamScanner] OCR недоступен (нет pytesseract/Pillow). Текстовый сканер активен.")
        else:
            tessdata_path = SCAM_TESSDATA_PATH
            if tessdata_path and os.path.isdir(tessdata_path):
                os.environ["TESSDATA_PREFIX"] = tessdata_path
                self.ready = True
                print(f"[ScamScanner] Запущен. Tessdata: {tessdata_path}")
            else:
                print(f"[ScamScanner] ОШИБКА: tessdata не найден: {tessdata_path}")

    @property
    def phrases(self) -> list:
        return store.get_phrases()

    async def on_message_received(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if str(message.channel.id) == self.notify_channel_id:
            return
        if isinstance(message.author, discord.Member):
            if message.author.guild_permissions.administrator:
                return
            if message.author.get_role(ROLE_STAFF_ID):
                return

        if store.is_blacklisted(message.author.id):
            try:
                await message.delete()
            except Exception:
                pass
            return

        if await self._check_flood(message):
            return

        text_matched = self._find_scam_phrase(message.content or "")
        link_matched = self._find_blacklisted_link(message.content or "")
        if text_matched or link_matched:
            reason = text_matched or f"ссылка: {link_matched}"
            await self._handle_scam(message, None, reason, is_image=False)
            return

        if not self.ready:
            return
        images = self._collect_images(message)
        if images:
            asyncio.create_task(self._scan_message(message, images))

    async def _check_flood(self, message: discord.Message) -> bool:
        content = (message.content or "").strip().lower()
        if len(content) < 3:
            return False
        uid = str(message.author.id)
        now = datetime.now(timezone.utc).timestamp()
        q = store.flood[uid]
        q.append((now, content))
        while q and now - q[0][0] > FLOOD_WINDOW_SECONDS:
            q.popleft()
        same = sum(1 for t, c in q if c == content)
        if same >= FLOOD_SAME_COUNT:
            try:
                await message.delete()
            except Exception:
                pass
            if isinstance(message.author, discord.Member):
                try:
                    until = datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MINUTES)
                    await message.author.timeout(until, reason="Анти-флуд: одинаковые сообщения")
                    notify = self.bot.get_channel(int(self.notify_channel_id))
                    if notify:
                        await notify.send(
                            f"🔇 {message.author.mention} мут {FLOOD_MUTE_MINUTES} мин (флуд одинаковых сообщений)"
                        )
                except Exception:
                    pass
            store.flood[uid].clear()
            return True
        return False

    def _find_blacklisted_link(self, text: str) -> Optional[str]:
        lower = text.lower()
        for link in SCAM_LINK_BLACKLIST:
            if link in lower:
                return link
        return None

    def _collect_images(self, message: discord.Message) -> list:
        result = []
        self._add_attachments(result, message.attachments)
        for embed in message.embeds:
            self._add_embed_images(result, embed)
        ref = message.reference
        if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
            self._add_attachments(result, ref.resolved.attachments)
        if hasattr(message, "message_snapshots"):
            for snap in message.message_snapshots:
                if hasattr(snap, "attachments"):
                    self._add_attachments(result, snap.attachments)
                if hasattr(snap, "embeds"):
                    for e in snap.embeds:
                        self._add_embed_images(result, e)
        return result

    @staticmethod
    def _add_embed_images(result, embed) -> None:
        if embed.image:
            url = embed.image.proxy_url or embed.image.url
            if url:
                result.append(ImageRef(url, "embed-image"))
        if embed.thumbnail:
            url = embed.thumbnail.proxy_url or embed.thumbnail.url
            if url:
                result.append(ImageRef(url, "embed-thumb"))

    @staticmethod
    def _add_attachments(result, attachments) -> None:
        for a in attachments:
            if not a.content_type or not a.content_type.startswith("image/"):
                continue
            if a.size > MAX_IMAGE_BYTES:
                continue
            url = a.url or a.proxy_url
            if url:
                result.append(ImageRef(url, a.filename))

    async def _scan_message(self, message, images) -> None:
        async with self._semaphore:
            for img in images:
                try:
                    bi = await self._download_image(img.url)
                    if bi is None:
                        continue
                    text = await asyncio.get_event_loop().run_in_executor(None, self._recognize, bi)
                    if not text or not text.strip():
                        continue
                    matched = self._find_scam_phrase(text)
                    if matched:
                        await self._handle_scam(message, img, matched, is_image=True)
                        return
                except Exception as e:
                    print(f"[ScamScanner] Ошибка: {e}")

    async def _download_image(self, url: str):
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AstrumTicketsScanner/1.0)",
            "Accept": "image/*,*/*;q=0.8",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(connect=5, total=20),
                                       allow_redirects=True) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.read()
            img = Image.open(io.BytesIO(data))
            img.load()
            return self._resize_if_needed(img)
        except Exception as e:
            print(f"[ScamScanner] Ошибка загрузки: {e}")
            return None

    @staticmethod
    def _resize_if_needed(src):
        w, h = src.size
        max_dim = max(w, h)
        if max_dim <= MAX_IMAGE_DIMENSION:
            return src
        scale = MAX_IMAGE_DIMENSION / max_dim
        return src.resize((round(w * scale), round(h * scale)), Image.BILINEAR)

    def _recognize(self, img) -> str:
        try:
            return pytesseract.image_to_string(img, config=self._tess_config)
        except Exception as e:
            print(f"[ScamScanner] OCR упал: {e}")
            return ""

    def _find_scam_phrase(self, text: str):
        if not text or not text.strip():
            return None
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        for phrase in self.phrases:
            if phrase in normalized:
                return phrase
        return None

    async def _handle_scam(self, message, img, phrase: str, is_image: bool = True) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        store.data["stats"]["scam_caught"] = store.data["stats"].get("scam_caught", 0) + 1
        uid = str(message.author.id)
        strikes = store.data["scam_strikes"].get(uid, 0) + 1
        store.data["scam_strikes"][uid] = strikes
        store.save()

        notify_channel = self.bot.get_channel(int(self.notify_channel_id))
        if notify_channel is None:
            return

        author = message.author
        embed = discord.Embed(
            title="🚨 Обнаружен спам" + (" на изображении" if is_image else " в тексте"),
            color=0xED4245,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Пользователь", value=f"{author.mention}\n`{author.id}`", inline=True)
        embed.add_field(name="Канал", value=f"<#{message.channel.id}>", inline=True)
        embed.add_field(name="Сработала фраза/ссылка", value=f"`{phrase}`", inline=True)
        if img:
            embed.add_field(name="Файл", value=img.file_name or "—", inline=False)
        embed.add_field(name="Страйков", value=f"`{strikes}` / {SCAM_MUTE_THRESHOLD}", inline=True)
        embed.set_footer(text="Сообщение удалено автоматически")

        try:
            await notify_channel.send(embed=embed)
        except discord.HTTPException:
            pass

        if strikes >= SCAM_MUTE_THRESHOLD and isinstance(author, discord.Member):
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=SCAM_MUTE_MINUTES)
                await author.timeout(until, reason=f"ScamScanner: {strikes} срабатываний")
                await notify_channel.send(
                    f"🔇 {author.mention} получил мут на {SCAM_MUTE_MINUTES} мин. (страйков: {strikes})"
                )
                store.data["scam_strikes"][uid] = 0
                store.save()
            except Exception:
                pass


class DeclineReasonModal(discord.ui.Modal, title="Причина отказа"):
    reason = discord.ui.TextInput(
        label="Причина отказа",
        placeholder="Укажите причину (будет отправлена заявителю)",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )

    def __init__(self, review_view):
        super().__init__()
        self.review_view = review_view

    async def on_submit(self, interaction: discord.Interaction):
        await self.review_view._resolve(interaction, accepted=False, reason=self.reason.value)


class ApplicationModal(discord.ui.Modal, title="📋 Заявка на должность"):
    nick = discord.ui.TextInput(label="Имя / Ник", placeholder="Введите ваш ник", max_length=64)
    age = discord.ui.TextInput(label="Возраст", placeholder="Введите ваш возраст", max_length=3)
    experience = discord.ui.TextInput(
        label="Был ли у вас опыт?",
        placeholder="Опишите ваш прошлый опыт (если есть)",
        style=discord.TextStyle.paragraph, max_length=512,
    )
    time_ready = discord.ui.TextInput(
        label="Сколько времени готовы уделять проекту?",
        placeholder="Например: 3-4 часа в день", max_length=128,
    )
    why = discord.ui.TextInput(
        label="Почему мы должны выбрать именно вас?",
        placeholder="Расскажите о себе",
        style=discord.TextStyle.paragraph, max_length=1024,
    )

    def __init__(self, role_choice: str):
        super().__init__()
        self.role_choice = role_choice

    async def on_submit(self, interaction: discord.Interaction):
        if store.is_blacklisted(interaction.user.id):
            await interaction.response.send_message("❌ Вы в чёрном списке.", ephemeral=True)
            return

        uid = str(interaction.user.id)
        last = store.data["application_cooldowns"].get(uid)
        if last:
            last_dt = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - last_dt < timedelta(days=APPLICATION_COOLDOWN_DAYS):
                remaining = APPLICATION_COOLDOWN_DAYS - (datetime.now(timezone.utc) - last_dt).days
                await interaction.response.send_message(
                    f"❌ Вы уже подавали заявку недавно. Повторная подача возможна через **{remaining}** дн.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True)
        log_channel = interaction.client.get_channel(APPLICATIONS_LOG_CHANNEL_ID)
        if log_channel is None:
            await interaction.followup.send("❌ Ошибка: канал для заявок не найден.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Новая заявка на должность",
            color=0xCD5C5C,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Discord", value=interaction.user.mention, inline=True)
        embed.add_field(name="💮 ID", value=f"`{interaction.user.id}`", inline=True)
        embed.add_field(name="🪪 Ник", value=self.nick.value, inline=True)
        embed.add_field(name="🎂 Возраст", value=self.age.value, inline=True)
        embed.add_field(name="🏷️ Должность", value=self.role_choice, inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name="📖 Опыт", value=self.experience.value, inline=False)
        embed.add_field(name="⏱️ Время на проект", value=self.time_ready.value, inline=False)
        embed.add_field(name="💬 Почему именно вы?", value=self.why.value, inline=False)
        embed.set_footer(text="Заявка ожидает рассмотрения")

        view = ApplicationReviewView(applicant_id=interaction.user.id, role_choice=self.role_choice)
        await log_channel.send(embed=embed, view=view)

        store.data["application_cooldowns"][uid] = datetime.now(timezone.utc).isoformat()
        store.save()

        await interaction.followup.send(
            "✅ Ваша заявка отправлена! Ожидайте ответа в течение 1-3 рабочих дней.",
            ephemeral=True,
        )


class ApplicationRoleSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="CS:GO", emoji=discord.PartialEmoji(name="12", id=1513719565777899581), value="CS:GO"),
            discord.SelectOption(label="CS2", emoji=discord.PartialEmoji(name="12", id=1513719565777899581), value="CS2"),
            discord.SelectOption(label="Discord Staff", emoji=discord.PartialEmoji(name="12", id=1513719565777899581), value="Discord Staff"),
        ]
        super().__init__(
            placeholder="Выберите должность для заявки...",
            min_values=1, max_values=1, options=options,
            custom_id="application_role_select",
        )

    async def callback(self, interaction: discord.Interaction):
        modal = ApplicationModal(role_choice=self.values[0])
        await interaction.response.send_modal(modal)


class ApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ApplicationRoleSelect())


class ApplicationReviewView(discord.ui.View):
    def __init__(self, applicant_id: int, role_choice: str = ""):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.role_choice = role_choice

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success, emoji="✅", custom_id="app_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, accepted=True)

    @discord.ui.button(label="Отказать", style=discord.ButtonStyle.danger, emoji="❌", custom_id="app_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = DeclineReasonModal(self)
        await interaction.response.send_modal(modal)

    async def _resolve(self, interaction: discord.Interaction, accepted: bool, reason: str = ""):
        color = 0x57F287 if accepted else 0xCD5C5C
        status = "✅ Принята" if accepted else "❌ Отказано"

        original_embed = interaction.message.embeds[0]
        new_embed = discord.Embed(title=original_embed.title, color=color, timestamp=original_embed.timestamp)
        for field in original_embed.fields:
            new_embed.add_field(name=field.name, value=field.value, inline=field.inline)
        footer_text = f"{status} • Рассмотрел: {interaction.user.display_name}"
        if reason:
            footer_text += f" | Причина: {reason[:80]}"
        new_embed.set_footer(text=footer_text)

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=new_embed, view=self)

        key = "applications_accepted" if accepted else "applications_declined"
        store.data["stats"][key] = store.data["stats"].get(key, 0) + 1
        store.save()

        if accepted and self.role_choice:
            role_id = APPLICATION_ROLE_MAP.get(self.role_choice)
            if role_id and interaction.guild:
                role = interaction.guild.get_role(role_id)
                member = interaction.guild.get_member(self.applicant_id)
                if role and member:
                    try:
                        await member.add_roles(role, reason="Заявка принята")
                    except discord.Forbidden:
                        pass

        await _audit(interaction.guild, f"{'✅ Принял' if accepted else '❌ Отклонил'} заявку", interaction.user, f"ID заявителя: {self.applicant_id}")

        applicant = interaction.client.get_user(self.applicant_id)
        if applicant:
            try:
                if accepted:
                    dm_desc = "🎉 Поздравляем! Ваша заявка была **принята**."
                else:
                    dm_desc = "😔 К сожалению, ваша заявка была **отклонена**."
                    if reason:
                        dm_desc += f"\n\n**Причина:** {reason}"
                dm_embed = discord.Embed(
                    title="📋 Ответ на вашу заявку",
                    description=dm_desc,
                    color=color,
                    timestamp=datetime.now(timezone.utc),
                )
                await applicant.send(embed=dm_embed)
            except discord.Forbidden:
                pass

        msg = f"{'✅ Заявка принята' if accepted else '❌ Заявка отклонена'}. Заявитель уведомлён в ЛС."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)


class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Задать вопрос", description="Исключительно вопросы, имеющие смысл.", emoji="🌺", value="question"),
            discord.SelectOption(label="Репорт-баг", description="Уведомить администрацию о найденном баге.", emoji="🛠️", value="bug"),
            discord.SelectOption(label="Апелляция блокировки", description="Подать заявку на апелляцию бана.", emoji="⚖️", value="appeal"),
            discord.SelectOption(label="Жалоба на игрока", description="Подать жалобу на нарушителя.", emoji="📝", value="report"),
        ]
        super().__init__(placeholder="Выберите категорию обращения",
                         min_values=1, max_values=1, options=options,
                         custom_id="ticket_select")

    async def callback(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user

        if store.is_blacklisted(user.id):
            await interaction.response.send_message("❌ Вы в чёрном списке и не можете открывать тикеты.", ephemeral=True)
            return

        if isinstance(user, discord.Member):
            for rid in PUNISHED_ROLE_IDS:
                if user.get_role(rid):
                    await interaction.response.send_message(
                        "❌ Вы не можете открыть тикет, пока у вас есть активное наказание.",
                        ephemeral=True,
                    )
                    return

        if store.count_open_tickets(user.id) >= TICKET_MAX_PER_USER:
            await interaction.response.send_message(
                f"❌ У вас уже есть открытый тикет. Максимум: **{TICKET_MAX_PER_USER}**.",
                ephemeral=True,
            )
            return

        staff_role = guild.get_role(ROLE_STAFF_ID)
        cat = guild.get_channel(CATEGORY_TICKETS_ID)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True,
                                              embed_links=True, attach_files=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        topic_ru = {"question": "вопрос", "bug": "баг",
                    "appeal": "апелляция", "report": "жалоба"}.get(self.values[0], "тикет")
        channel = await guild.create_text_channel(
            name=f"ticket-{topic_ru}-{user.name}", category=cat, overwrites=overwrites)

        await interaction.response.edit_message(view=TicketView())
        await channel.send(
            embed=discord.Embed(
                title=f"🎫 Тикет открыт! ({topic_ru.capitalize()})",
                description=(
                    f"Приветствуем, {user.mention}!\n"
                    f"Опишите вашу проблему максимально подробно.\n\n"
                    f"**Пожалуйста, не пингуйте администрацию** — вам ответят, как только освободятся."
                ),
                color=0xCD5C5C,
            ),
            view=TicketControlView(),
        )
        await interaction.followup.send(f"Ваш тикет: {channel.mention}", ephemeral=True)

        now_iso = datetime.now(timezone.utc).isoformat()
        store.data["stats"]["tickets_opened"] = store.data["stats"].get("tickets_opened", 0) + 1
        store.data["ticket_last_activity"][str(channel.id)] = now_iso
        store.data["ticket_owners"][str(channel.id)] = user.id
        store.data["ticket_opened_at"][str(channel.id)] = now_iso
        store.save()

        log_ch = guild.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            await log_ch.send(embed=discord.Embed(
                title="🟩 Тикет открыт",
                description=f"Пользователь: {user.mention}\nКанал: {channel.mention}\nТема: {topic_ru}",
                color=0x00FF00,
            ))


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())


class AddMemberModal(discord.ui.Modal, title="Добавить участника в тикет"):
    user_id = discord.ui.TextInput(
        label="ID пользователя",
        placeholder="Введите Discord ID",
        max_length=20,
        required=True,
    )

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Некорректный ID.", ephemeral=True)
            return
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Пользователь не найден на сервере.", ephemeral=True)
            return
        await self.channel.set_permissions(member, read_messages=True, send_messages=True,
                                           embed_links=True, attach_files=True)
        await interaction.response.send_message(f"✅ {member.mention} добавлен в тикет.", ephemeral=True)
        await self.channel.send(f"➕ {member.mention} был добавлен в тикет.")
        await _audit(interaction.guild, "Добавил участника в тикет", interaction.user, f"{member} → {self.channel.name}")


class TransferModal(discord.ui.Modal, title="Передать тикет"):
    staff_id = discord.ui.TextInput(
        label="ID модератора",
        placeholder="Введите Discord ID модератора",
        max_length=20,
        required=True,
    )

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.staff_id.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Некорректный ID.", ephemeral=True)
            return
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("❌ Пользователь не найден.", ephemeral=True)
            return
        store.data["ticket_claims"][str(self.channel.id)] = uid
        store.save()
        await interaction.response.send_message(f"✅ Тикет передан {member.mention}.", ephemeral=True)
        await self.channel.send(f"🔄 Тикет передан модератору {member.mention}.")
        await _set_ticket_emoji(self.channel, "🟢")
        await _audit(interaction.guild, "Передал тикет", interaction.user, f"→ {member} | {self.channel.name}")


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Закрыть тикет", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_staff(interaction):
            await interaction.response.send_message("❌ Закрывать тикет может только администрация.", ephemeral=True)
            return
        await _close_ticket(interaction, reason="Закрыт кнопкой")

    @discord.ui.button(label="Взять тикет", style=discord.ButtonStyle.primary, emoji="✋", custom_id="claim_ticket")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_staff(interaction):
            await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
            return
        cid = str(interaction.channel.id)
        store.data["ticket_claims"][cid] = interaction.user.id
        store.save()
        await interaction.response.send_message(f"✅ Тикет взят: {interaction.user.mention}")
        await _set_ticket_emoji(interaction.channel, "🟢")
        try:
            async for msg in interaction.channel.history(limit=5, oldest_first=True):
                if msg.embeds and msg.author == interaction.client.user:
                    emb = msg.embeds[0]
                    new_emb = discord.Embed(
                        title=emb.title,
                        description=(emb.description or "") + f"\n\n**Взял:** {interaction.user.mention}",
                        color=emb.color or 0xCD5C5C,
                    )
                    await msg.edit(embed=new_emb)
                    break
        except Exception:
            pass
        await _audit(interaction.guild, "Взял тикет", interaction.user, interaction.channel.name)

    @discord.ui.button(label="Добавить участника", style=discord.ButtonStyle.secondary, emoji="➕", custom_id="add_member_ticket")
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_staff(interaction):
            await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
            return
        await interaction.response.send_modal(AddMemberModal(interaction.channel))

    @discord.ui.button(label="Передать", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="transfer_ticket")
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_staff(interaction):
            await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
            return
        await interaction.response.send_modal(TransferModal(interaction.channel))

    @discord.ui.button(label="Решено", style=discord.ButtonStyle.success, emoji="✅", custom_id="resolve_ticket")
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_staff(interaction):
            await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
            return
        await _set_ticket_emoji(interaction.channel, "✅")
        await interaction.response.send_message("✅ Тикет помечен как **решённый**. Можно закрывать.", ephemeral=False)
        try:
            async for msg in interaction.channel.history(limit=5, oldest_first=True):
                if msg.embeds and msg.author == interaction.client.user:
                    emb = msg.embeds[0]
                    new_emb = discord.Embed(
                        title=emb.title,
                        description=(emb.description or "") + "\n\n**Статус: ✅ Решено**",
                        color=0x57F287,
                    )
                    await msg.edit(embed=new_emb)
                    break
        except Exception:
            pass
        await _audit(interaction.guild, "Пометил тикет как решённый", interaction.user, interaction.channel.name)


def _is_staff(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    if isinstance(interaction.user, discord.Member) and interaction.user.get_role(ROLE_STAFF_ID):
        return True
    return False


async def _set_ticket_emoji(channel, emoji: str):
    name = channel.name
    for e in ("🟢", "🔴", "✅", "⏰"):
        if name.startswith(e + "-"):
            name = name[len(e) + 1:]
            break
    if name.startswith("ticket-"):
        new_name = f"{emoji}-{name}"
    else:
        new_name = f"{emoji}-ticket-{name}"
    try:
        await channel.edit(name=new_name[:100])
    except Exception:
        pass


async def _audit(guild, action: str, user, details: str = ""):
    if not guild:
        return
    ch = guild.get_channel(AUDIT_CHANNEL_ID)
    if not ch:
        return
    emb = discord.Embed(
        title="📝 Аудит",
        description=f"**Действие:** {action}\n**Кто:** {user.mention} (`{user.id}`)\n**Детали:** {details or '—'}",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await ch.send(embed=emb)
    except Exception:
        pass


async def _close_ticket(interaction: discord.Interaction, reason: str = ""):
    channel = interaction.channel
    guild = interaction.guild

    try:
        await channel.edit(name=f"closed-{reason[:20]}-{channel.name}"[:100])
    except Exception:
        pass

    transcript_lines = []
    try:
        async for msg in channel.history(limit=400, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
            content = msg.content or ""
            if msg.attachments:
                content += " " + " ".join(a.url for a in msg.attachments)
            transcript_lines.append(f"[{ts}] {msg.author}: {content}")
    except Exception:
        pass
    transcript_text = "\n".join(transcript_lines) if transcript_lines else "Нет сообщений."

    log_ch = guild.get_channel(LOG_CHANNEL_ID)
    if log_ch:
        embed = discord.Embed(
            title="🟥 Тикет закрыт",
            description=(
                f"Закрыл: {interaction.user.mention}\n"
                f"Канал: `{channel.name}`\n"
                f"Причина: {reason or '—'}"
            ),
            color=0xFF0000,
            timestamp=datetime.now(timezone.utc),
        )
        if len(transcript_text) < 1800:
            embed.add_field(name="Транскрипт", value=f"```\n{transcript_text[:1800]}\n```", inline=False)
            await log_ch.send(embed=embed)
        else:
            file = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=f"transcript-{channel.id}.txt")
            await log_ch.send(embed=embed, file=file)

    owner_id = store.data.get("ticket_owners", {}).get(str(channel.id))
    if owner_id:
        owner = interaction.client.get_user(int(owner_id))
        if owner:
            try:
                dm_emb = discord.Embed(
                    title="📄 Ваш тикет закрыт",
                    description=f"**Причина:** {reason or '—'}\n\nНиже — копия переписки.",
                    color=0xCD5C5C,
                    timestamp=datetime.now(timezone.utc),
                )
                if len(transcript_text) < 1800:
                    dm_emb.add_field(name="Транскрипт", value=f"```\n{transcript_text[:1800]}\n```", inline=False)
                    await owner.send(embed=dm_emb)
                else:
                    f = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename="transcript.txt")
                    await owner.send(embed=dm_emb, file=f)
            except discord.Forbidden:
                pass

    store.data["stats"]["tickets_closed"] = store.data["stats"].get("tickets_closed", 0) + 1
    for key in ("ticket_claims", "ticket_last_activity", "ticket_owners", "ticket_opened_at"):
        store.data[key].pop(str(channel.id), None)
    store.save()

    await _audit(guild, "Закрыл тикет", interaction.user, f"{channel.name} | {reason}")

    if not interaction.response.is_done():
        await interaction.response.send_message("Тикет будет удалён через 5 секунд...", ephemeral=False)
    else:
        await interaction.followup.send("Тикет будет удалён через 5 секунд...")
    await asyncio.sleep(5)
    try:
        await channel.delete()
    except discord.HTTPException:
        pass


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.scam_scanner = ScamImageScanner(self)

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(TicketControlView())
        self.add_view(ApplicationView())
        self.add_view(ApplicationReviewView(applicant_id=0))
        self.auto_close_tickets.start()
        self.check_stale_applications.start()
        self.user_reminders.start()
        self.update_status.start()

    @tasks.loop(minutes=30)
    async def auto_close_tickets(self):
        await self.wait_until_ready()
        guild = self.get_guild(GUILD_ID)
        if not guild:
            return
        cat = guild.get_channel(CATEGORY_TICKETS_ID)
        if not cat or not isinstance(cat, discord.CategoryChannel):
            return
        now = datetime.now(timezone.utc)
        for channel in list(cat.text_channels):
            if "ticket-" not in channel.name and not channel.name.startswith(("🟢", "🔴", "✅", "⏰")):
                continue
            last = store.data["ticket_last_activity"].get(str(channel.id))
            if not last:
                store.data["ticket_last_activity"][str(channel.id)] = now.isoformat()
                continue
            last_dt = datetime.fromisoformat(last)
            if now - last_dt > timedelta(hours=TICKET_INACTIVE_HOURS):
                try:
                    await channel.send(embed=discord.Embed(
                        title="⏰ Тикет закрыт автоматически",
                        description=f"Неактивен более {TICKET_INACTIVE_HOURS} часов.",
                        color=0xFFAA00,
                    ))
                    transcript_lines = []
                    async for msg in channel.history(limit=200, oldest_first=True):
                        ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
                        transcript_lines.append(f"[{ts}] {msg.author}: {msg.content or ''}")
                    transcript_text = "\n".join(transcript_lines)
                    log_ch = guild.get_channel(LOG_CHANNEL_ID)
                    if log_ch:
                        emb = discord.Embed(
                            title="🟥 Тикет закрыт (авто)",
                            description=f"Канал: `{channel.name}`\nПричина: неактивность {TICKET_INACTIVE_HOURS}ч",
                            color=0xFFAA00,
                        )
                        if len(transcript_text) < 1800:
                            emb.add_field(name="Транскрипт", value=f"```\n{transcript_text[:1800]}\n```", inline=False)
                            await log_ch.send(embed=emb)
                        else:
                            f = discord.File(io.BytesIO(transcript_text.encode()), filename=f"transcript-{channel.id}.txt")
                            await log_ch.send(embed=emb, file=f)

                    owner_id = store.data.get("ticket_owners", {}).get(str(channel.id))
                    if owner_id:
                        owner = self.get_user(int(owner_id))
                        if owner:
                            try:
                                await owner.send(embed=discord.Embed(
                                    title="⏰ Тикет закрыт автоматически",
                                    description=f"Тикет был закрыт из-за неактивности ({TICKET_INACTIVE_HOURS}ч).",
                                    color=0xFFAA00,
                                ))
                            except Exception:
                                pass

                    store.data["stats"]["tickets_closed"] = store.data["stats"].get("tickets_closed", 0) + 1
                    for key in ("ticket_claims", "ticket_last_activity", "ticket_owners", "ticket_opened_at"):
                        store.data[key].pop(str(channel.id), None)
                    store.save()
                    await asyncio.sleep(2)
                    await channel.delete()
                except Exception as e:
                    log.error(f"Автозакрытие {channel.id}: {e}")

    @tasks.loop(hours=6)
    async def check_stale_applications(self):
        await self.wait_until_ready()
        log_channel = self.get_channel(APPLICATIONS_LOG_CHANNEL_ID)
        if not log_channel:
            return
        try:
            async for msg in log_channel.history(limit=30):
                if not msg.embeds or not msg.components:
                    continue
                emb = msg.embeds[0]
                if "Новая заявка" not in (emb.title or ""):
                    continue
                age = datetime.now(timezone.utc) - (emb.timestamp or msg.created_at)
                if age > timedelta(hours=APPLICATION_STALE_HOURS):
                    await log_channel.send(
                        f"⚠️ Заявка ожидает рассмотрения уже **{int(age.total_seconds() // 3600)}** ч.\n{msg.jump_url}"
                    )
        except Exception as e:
            log.error(f"check_stale_applications: {e}")

    @tasks.loop(hours=1)
    async def user_reminders(self):
        await self.wait_until_ready()
        guild = self.get_guild(GUILD_ID)
        if not guild:
            return
        cat = guild.get_channel(CATEGORY_TICKETS_ID)
        if not cat:
            return
        now = datetime.now(timezone.utc)
        for channel in cat.text_channels:
            last = store.data["ticket_last_activity"].get(str(channel.id))
            if not last:
                continue
            last_dt = datetime.fromisoformat(last)
            if now - last_dt < timedelta(hours=TICKET_USER_REMIND_HOURS):
                continue
            try:
                already = False
                async for msg in channel.history(limit=3):
                    if msg.author == self.user and "Вы ещё здесь?" in (msg.content or ""):
                        already = True
                        break
                if not already:
                    owner_id = store.data.get("ticket_owners", {}).get(str(channel.id))
                    mention = f"<@{owner_id}>" if owner_id else ""
                    await channel.send(f"{mention} 👋 Вы ещё здесь? Если вопрос решён — напишите, тикет можно закрыть.")
            except Exception:
                pass

    @tasks.loop(minutes=5)
    async def update_status(self):
        await self.wait_until_ready()
        open_count = len(store.data.get("ticket_owners", {}))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"тикетов: {open_count}",
            )
        )


bot = Bot()


@bot.event
async def on_ready():
    print(f"[Bot] {bot.user.name} запущен | ID: {bot.user.id}")
    if bot.scam_scanner.ready:
        print("[Bot] ScamScanner: OCR активен")
    else:
        print("[Bot] ScamScanner: OCR выключен (текст/ссылки работают)")

    # Проверяем, видит ли бот сервер
    g = bot.get_guild(GUILD_ID)
    if g is None:
        print(f"[Bot] ⚠️ Сервер GUILD_ID={GUILD_ID} не найден. "
              f"Бот не на сервере или неверный ID. Гильдий в кэше: {len(bot.guilds)}")
        for gg in bot.guilds:
            print(f"       - {gg.name} ({gg.id})")
    else:
        print(f"[Bot] Сервер: {g.name} ({g.id})")

    # Синхронизация slash-команд (не роняем бота при ошибке)
    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"[Bot] Slash-команды синхронизированы: {len(synced)}")
    except discord.Forbidden as e:
        print(
            "[Bot] ❌ Нет доступа к синхронизации команд (403 Missing Access).\n"
            "     Решение:\n"
            "     1) Перепригласите бота со scope: bot + applications.commands\n"
            "        https://discord.com/oauth2/authorize?client_id=CLIENT_ID&permissions=8&scope=bot%20applications.commands\n"
            "     2) Проверьте GUILD_ID в конфиге\n"
            "     3) Убедитесь, что токен от того же приложения, что приглашено на сервер\n"
            f"     Детали: {e}"
        )
    except Exception as e:
        print(f"[Bot] ❌ Ошибка sync команд: {type(e).__name__}: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.guild and message.channel.name:
        is_ticket = (
            "ticket-" in message.channel.name
            or message.channel.name.startswith(("🟢", "🔴", "✅", "⏰", "closed-"))
        )
        if is_ticket:
            store.data["ticket_last_activity"][str(message.channel.id)] = datetime.now(timezone.utc).isoformat()
            if not message.author.bot:
                store.save()
            if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                try:
                    await message.delete()
                except Exception:
                    pass

    if (
        not message.author.bot
        and message.guild
        and message.content
        and not (message.channel.name and ("ticket-" in message.channel.name or message.channel.name.startswith(("🟢", "🔴", "✅", "⏰"))))
    ):
        lower = message.content.lower()
        for key, reply in AUTO_REPLIES.items():
            if key in lower:
                try:
                    await message.reply(reply, mention_author=False)
                except Exception:
                    pass
                break

    await bot.scam_scanner.on_message_received(message)
    await bot.process_commands(message)


def _is_ticket_channel(channel) -> bool:
    name = getattr(channel, "name", "") or ""
    return "ticket-" in name or name.startswith(("🟢", "🔴", "✅", "⏰", "closed-"))


@bot.tree.command(name="close", description="Закрыть текущий тикет с причиной")
@app_commands.describe(reason="Причина закрытия")
async def close_cmd(interaction: discord.Interaction, reason: str = "Закрыт командой"):
    if not _is_ticket_channel(interaction.channel):
        await interaction.response.send_message("❌ Только в тикетах.", ephemeral=True)
        return
    if not _is_staff(interaction):
        await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
        return
    await _close_ticket(interaction, reason=reason)


@bot.tree.command(name="rename", description="Переименовать текущий тикет")
@app_commands.describe(new_name="Новое имя (без префиксов)")
async def rename_cmd(interaction: discord.Interaction, new_name: str):
    if not _is_ticket_channel(interaction.channel):
        await interaction.response.send_message("❌ Только в тикетах.", ephemeral=True)
        return
    if not _is_staff(interaction):
        await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
        return
    clean = re.sub(r"[^a-zA-Zа-яА-Я0-9\-_]", "", new_name)[:70]
    await interaction.channel.edit(name=f"ticket-{clean}")
    await interaction.response.send_message(f"✅ Переименован в `ticket-{clean}`", ephemeral=True)


@bot.tree.command(name="ticket", description="Информация о текущем тикете")
async def ticket_info(interaction: discord.Interaction):
    if not _is_ticket_channel(interaction.channel):
        await interaction.response.send_message("❌ Только в тикетах.", ephemeral=True)
        return
    cid = str(interaction.channel.id)
    owner_id = store.data.get("ticket_owners", {}).get(cid)
    claim_id = store.data.get("ticket_claims", {}).get(cid)
    opened = store.data.get("ticket_opened_at", {}).get(cid)
    last = store.data.get("ticket_last_activity", {}).get(cid)

    emb = discord.Embed(title="ℹ️ Информация о тикете", color=0xCD5C5C)
    emb.add_field(name="Канал", value=interaction.channel.mention, inline=True)
    emb.add_field(name="Владелец", value=f"<@{owner_id}>" if owner_id else "—", inline=True)
    emb.add_field(name="Взял", value=f"<@{claim_id}>" if claim_id else "Никто", inline=True)
    if opened:
        emb.add_field(name="Открыт", value=f"<t:{int(datetime.fromisoformat(opened).timestamp())}:R>", inline=True)
    if last:
        emb.add_field(name="Активность", value=f"<t:{int(datetime.fromisoformat(last).timestamp())}:R>", inline=True)
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="stats", description="Статистика бота")
async def stats_cmd(interaction: discord.Interaction):
    if not _is_staff(interaction):
        await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
        return
    s = store.data.get("stats", {})
    open_count = len(store.data.get("ticket_owners", {}))
    bl_count = len(store.data.get("blacklist", []))
    embed = discord.Embed(title="📊 Статистика", color=0xCD5C5C, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Тикеты открыто (всего)", value=str(s.get("tickets_opened", 0)), inline=True)
    embed.add_field(name="Тикеты закрыто", value=str(s.get("tickets_closed", 0)), inline=True)
    embed.add_field(name="Сейчас открыто", value=str(open_count), inline=True)
    embed.add_field(name="Заявки принято", value=str(s.get("applications_accepted", 0)), inline=True)
    embed.add_field(name="Заявки отклонено", value=str(s.get("applications_declined", 0)), inline=True)
    embed.add_field(name="Скам поймано", value=str(s.get("scam_caught", 0)), inline=True)
    embed.add_field(name="Чёрный список", value=str(bl_count), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="blacklist", description="Управление чёрным списком")
@app_commands.describe(action="add / remove / list", user="Пользователь")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list", value="list"),
])
async def blacklist_cmd(interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.User = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Только для администраторов.", ephemeral=True)
        return
    act = action.value
    if act == "list":
        bl = store.data.get("blacklist", [])
        text = ", ".join(f"`{uid}`" for uid in bl[:30]) or "пусто"
        await interaction.response.send_message(f"**Чёрный список ({len(bl)}):**\n{text}", ephemeral=True)
        return
    if not user:
        await interaction.response.send_message("Укажите пользователя.", ephemeral=True)
        return
    if act == "add":
        ok = store.blacklist_add(user.id)
        await interaction.response.send_message(
            f"{'✅ Добавлен в ЧС' if ok else '⚠️ Уже в ЧС'}: {user.mention}",
            ephemeral=True,
        )
        await _audit(interaction.guild, "Добавил в чёрный список", interaction.user, str(user))
    else:
        ok = store.blacklist_remove(user.id)
        await interaction.response.send_message(
            f"{'✅ Убран из ЧС' if ok else '⚠️ Не был в ЧС'}: {user.mention}",
            ephemeral=True,
        )


@bot.tree.command(name="scam", description="Управление сканером")
@app_commands.describe(action="Действие", phrase="Фраза", user="Для reset_strikes")
@app_commands.choices(action=[
    app_commands.Choice(name="add_phrase", value="add"),
    app_commands.Choice(name="remove_phrase", value="remove"),
    app_commands.Choice(name="list", value="list"),
    app_commands.Choice(name="reset_strikes", value="reset"),
])
async def scam_cmd(interaction: discord.Interaction, action: app_commands.Choice[str], phrase: str = "", user: discord.User = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Только для администраторов.", ephemeral=True)
        return
    act = action.value
    if act == "add":
        if not phrase:
            await interaction.response.send_message("Укажите фразу.", ephemeral=True)
            return
        ok = store.add_phrase(phrase)
        await interaction.response.send_message(f"{'✅ Добавлено' if ok else '⚠️ Уже есть'}: `{phrase.lower().strip()}`", ephemeral=True)
    elif act == "remove":
        if not phrase:
            await interaction.response.send_message("Укажите фразу.", ephemeral=True)
            return
        ok = store.remove_phrase(phrase)
        await interaction.response.send_message(f"{'✅ Удалено' if ok else '⚠️ Не найдено'}: `{phrase.lower().strip()}`", ephemeral=True)
    elif act == "list":
        phrases = store.get_phrases()
        text = ", ".join(f"`{p}`" for p in phrases[:50])
        if len(phrases) > 50:
            text += f" … +{len(phrases)-50}"
        await interaction.response.send_message(f"**Фразы ({len(phrases)}):**\n{text}", ephemeral=True)
    elif act == "reset":
        if not user:
            await interaction.response.send_message("Укажите пользователя.", ephemeral=True)
            return
        store.data["scam_strikes"].pop(str(user.id), None)
        store.save()
        await interaction.response.send_message(f"✅ Страйки {user.mention} сброшены.", ephemeral=True)


@bot.tree.command(name="phrases", description="Список фраз сканера (для модераторов)")
async def phrases_cmd(interaction: discord.Interaction):
    if not _is_staff(interaction):
        await interaction.response.send_message("❌ Только для стаффа.", ephemeral=True)
        return
    phrases = store.get_phrases()
    text = ", ".join(f"`{p}`" for p in phrases[:40])
    if len(phrases) > 40:
        text += f" … +{len(phrases)-40}"
    await interaction.response.send_message(f"**Фразы сканера ({len(phrases)}):**\n{text}", ephemeral=True)


@bot.tree.command(name="faq_scwh", description="вся система поддержки")
@app_commands.checks.has_permissions(administrator=True)
async def faq_scwh(interaction: discord.Interaction):
    await interaction.response.send_message("Система поддержки успешно развернута!", ephemeral=True)
    channel = interaction.channel
    tickets_mention = f"<#{channel.id}>"
    embed1 = discord.Embed(color=0xFF66B2)
    if BANNER_URL:
        embed1.set_image(url=BANNER_URL)
    embed2 = discord.Embed(
        title="– Всё, что связано с заходом на Сервер:",
        description=(
            f"**Основная информация о сервере находится в данном канале:** <#{INFO_CHANNEL_ID}>\n\n"
            "**1. Ошибки с подключением:**\n– Конкретного решения нет, но есть способы:\n"
            "\u200b- Отключите все программы, влияющие на подключение.\n– Очистите ДНС кэш.\n"
            "\u200b- Если не помогло, попробуйте VPN.\n"
            f"\u200b- Вообще Ничего не помогло? Обратитесь в: {tickets_mention}"
        ),
        color=0xFF66B2,
    ).set_image(url=SEPARATOR_URL)
    embed3 = discord.Embed(
        title="– Всё, что связано с сайтом:",
        description=(
            "**2. Проблемы с авторизацией или доступом:**\n"
            "\u200b- Убедитесь, что вы используете корректные данные для входа.\n"
            "\u200b- Если сайт отображается некорректно, попробуйте очистить кэш браузера или воспользоваться режимом «Инкогнито».\n\n"
            "**2.1. Сайт выдает ошибку:**\n\n"
            f"\u200b- Сделайте скриншот ошибки и откройте обращение в {tickets_mention}. "
            "Обязательно опишите, какие действия привели к ошибке, чтобы быстрее вам помогли."
        ),
        color=0xFF66B2,
    ).set_image(url=SEPARATOR_URL)
    embed4 = discord.Embed(
        title="– Прочее:",
        description=(
            f"**3. Я нашёл баг, что мне делать?**\n– Если вы нашли баг, следует как можно скорее обратиться в {tickets_mention}\n\n"
            f"**3.1 Я незаслуженно получил наказание на сервере.**\n\n\u200b- Каждый имеет право подать апелляцию на полученное наказание, в канале: {tickets_mention}"
        ),
        color=0xFF66B2,
    ).set_image(url=SEPARATOR_URL)
    embed_ticket = discord.Embed(
        title="📑 - ОБРАЩЕНИЕ В ПОДДЕРЖКУ",
        description="Перед тем, как открыть тикет, пожалуйста, ознакомьтесь с вышеприведённой информацией.",
        color=0xFF66B2,
    )
    embed_ticket.set_footer(
        text="ZXC CS:GO SUPPORT",
        icon_url=interaction.guild.icon.url if interaction.guild.icon else None,
    )
    await channel.send(embeds=[embed1, embed2, embed3, embed4, embed_ticket], view=TicketView())


@bot.tree.command(name="rules_scwh", description="Полный свод правил")
@app_commands.checks.has_permissions(administrator=True)
async def rules_scwh(interaction: discord.Interaction):
    await interaction.response.send_message("Развертывание правил...", ephemeral=True)
    rules_text = (
        "**1. Уважайте других участников**\nОбщайтесь вежливо, избегайте оскорблений, оскорбительных выражений и личных нападок.\n\n"
        "**2. Запрещена ненормативная лексика и оскорбления**\nПоддерживайте позитивную атмосферу, избегайте грубых выражений и ругательств.\n\n"
        "**3. Соблюдайте тему каналов**\nПишите по теме соответствующего канала и не создавайте оффтопика без необходимости.\n\n"
        "**4. Запрещена реклама и спам**\nНе размещайте рекламу сторонних ресурсов, ботов или своих проектов без разрешения администрации.\n\n"
        "**5. Не делитесь личной информацией**\nУважайте приватность других участников, не публикуйте их личные данные без согласия.\n\n"
        "**6. Соблюдайте правила Discord и законы**\nНе поощряйте незаконную деятельность и не нарушайте правила платформы.\n\n"
        "**7. Используйте разрешённые стикеры, эмодзи и медиа**\nНе размещайте запрещённый контент, такие как порнография, насилие и т.п.\n\n"
        "**8. Администрация оставляет за собой право удалять сообщения и блокировать участников при необходимости.**\n\n"
        "**9. Не обсуждайте запрещённые темы**\nИзбегайте разговоров о политике, религии, провокационных темах, вызывающих конфликты.\n\n"
        "**10. Соблюдайте правила языкового поведения**\nПишите на том языке, который выбран для сервера, избегайте спама и капса."
    )
    rules_text2 = (
        "**11. Используйте соответствующие роли и каналы**\nНе пишите в каналах, предназначенных для определённых целей, если у вас нет соответствующих прав.\n\n"
        "**12. Не поощряйте токсичность и троллинг**\nОбщайтесь конструктивно, не провоцируйте конфликтов и не провоцируйте других участников.\n\n"
        "**13. Соблюдайте правила использования ботов**\nСледите за корректностью команд и не злоупотребляйте функциями ботов.\n\n"
        "**14. Обращайтесь к администрации при возникновении проблем**\nЕсли у вас возникнут конфликты или вопросы, обращайтесь к модераторам или администраторам.\n\n"
        "**15. Не создавайте флейм и споры**\nСледите за тоном общения, избегайте провокаций и конфликтных ситуаций.\n\n"
        "**16. Не размещайте ссылки на вредоносное ПО или фишинг**\nЭто строго запрещено и карается блокировкой.\n\n"
        "**17. Соблюдайте авторские права и интеллектуальную собственность**\nНе публикуйте материалы, нарушающие авторские права, и уважайте чужую работу.\n\n"
        "**18. Используйте аватары и никнеймы в соответствии с правилами**\nИзбегайте оскорбительных, провокационных или неподходящих изображений и имён.\n\n"
        "**19. Не создавайте дублирующие аккаунты без необходимости**\nЭто может привести к ограничению доступа или блокировке.\n\n"
        "**20. Поддерживайте чистоту чатов и каналов**\nУдаляйте свои старые сообщения, если они устарели или неактуальны.\n\n"
        "**21. Не устраивайте массовых сообщений или флуд**\nЭто мешает другим участникам и нарушает правила сервера.\n\n"
        "**22. Следите за обновлениями правил и уведомлений**\nАдминистрация может вносить изменения - будьте в курсе."
    )
    admin_code = (
        "**Кодекс Администратора проекта [ZXC CS2/CSGO]**\n\n"
        "**Общие положения**\n1.1. Администратор обязан знать и соблюдать правила.\n1.2. Админка - это помощь, а не привилегия.\n1.3. ГА обладает правом вето.\n\n"
        "**Активность**\n2.1. Учет времени автоматический.\n2.2. Авто-снятие через 7 дней неактивности.\n2.3. АФК не засчитывается."
    )
    e_b = discord.Embed(color=0xCD5C5C)
    if RUBANNER_URL:
        e_b.set_image(url=RUBANNER_URL)
    e1 = discord.Embed(title="📜 ПРАВИЛА ДИСКОРД СЕРВЕРА 1-10", description=rules_text, color=0xCD5C5C).set_image(url=SEPARATOR_URL)
    e2 = discord.Embed(title="📜 ПРАВИЛА ДИСКОРД СЕРВЕРА 11-22", description=rules_text2, color=0xCD5C5C).set_image(url=SEPARATOR_URL)
    e3 = discord.Embed(title="🛡️ Кодекс Администратора", description=admin_code, color=0xCD5C5C).set_image(url=SEPARATOR_URL)
    await interaction.channel.send(embeds=[e_b, e1, e2, e3])


@bot.tree.command(name="faq_dolsh", description="Требования к должностным лицам")
@app_commands.checks.has_permissions(administrator=True)
async def faq_dolsh(interaction: discord.Interaction):
    await interaction.response.send_message("Развертывание FAQ...", ephemeral=True)
    e_b = discord.Embed(title="Требования становления на должностное лицо проекта", color=0xCD5C5C)
    if RUBANNER_URL:
        e_b.set_image(url=RUBANNER_URL)
    text1 = (
        "<@&1506343897046061208> / <@&1506350310803771523>\n"
        "1. — Вы строго должны знать правила проекта.\n"
        "2. — Вы являетесь высшей администрацией на своём режиме.\n"
        "3. — Вы обязаны обучать и назначать должностных.\n"
        "4. — Вы отвечаете за свой режим и должностных своего режима.\n"
        "5. — Вы можете проверять на всех серверах своего режима.\n"
        "6. — Вы должны проводить тестирование 1 раз в 3 недели.\n"
        "7. — Вы обязаны вносить позитивную и адекватную атмосферу на серверах проекта."
    )
    text2 = (
        "<@&1512315658216538193> / <@&1500474195211980981>\n"
        "2. — Вы строго должны знать правила проекта.\n"
        "3. — Вы можете проверять на всех серверах своего режима.\n"
        "4. — Вы являетесь помощником должностных по рангу ниже.\n"
        "5. — Вы обязаны вносить позитивную и адекватную атмосферу на серверах проекта."
    )
    text3 = (
        "<@&1506373750319349810> / <@&1506374186942333018>\n"
        "1. — Вы строго должны знать правила проекта.\n"
        "2. — Вы можете проверять только на своём сервере.\n"
        "3. — Вы можете назначать модераторов на свой сервер (После проверки на ПО и согласования с руководством).\n"
        "4. — Вы обязаны следить и обучать своих должностных.\n"
        "5. — Вы обязаны вносить позитивную и адекватную атмосферу на серверах проекта."
    )
    text4 = (
        "<@&1506351245667860540> / <@&1506373876052131900>\n"
        "1. — Вы строго должны знать правила проекта.\n"
        "2. — Вы обязаны вносить позитивную и адекватную атмосферу на серверах проекта.\n"
        "3. — Вы обязаны следить за недопущением нарушений на своём сервере.\n"
        "4. — У Вас должен быть хороший микрофон.\n"
        "5. — Вам должно быть 15 или более лет."
    )
    e1 = discord.Embed(description=text1, color=0xCD5C5C).set_image(url=SEPARATOR_URL)
    e2 = discord.Embed(description=text2, color=0xCD5C5C).set_image(url=SEPARATOR_URL)
    e3 = discord.Embed(description=text3, color=0xCD5C5C).set_image(url=SEPARATOR_URL)
    e4 = discord.Embed(description=text4, color=0xCD5C5C).set_image(url=SEPARATOR_URL)
    await interaction.channel.send(embeds=[e_b, e1, e2, e3, e4])


@bot.tree.command(name="applications_scwh", description="Разместить систему заявок на должность")
@app_commands.checks.has_permissions(administrator=True)
async def applications_scwh(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Система заявок развёрнута!", ephemeral=True)
    log_channel_mention = f"<#{APPLICATIONS_LOG_CHANNEL_ID}>"
    embed = discord.Embed(
        title="📚 Заявки на должность",
        description=(
            "ㅤ\n"
            "Заявку можно заполнить, нажав на выпадающий список ниже в этом канале.\n\n"
            f"Как только Вы заполните данную заявку, то она отправится в канал {log_channel_mention}\n\n"
            f"Когда кто-то из кураторов ознакомится с вашей заявкой, то с вами свяжутся или ответят на заявку в {log_channel_mention}\n\n"
            "Заявки рассматриваются в течение **1-3х рабочих дней**.\n\n"
            "Если вы готовы вносить позитивную и адекватную атмосферу, а так же быть стражем дискорд сервера, то мы ждём ваших заявок.\n\n"
            "Если Вы заполнили заявку, ожидайте на неё ответа — не стоит писать в лс и спрашивать, когда вам ответят на заявку."
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="ZXC CS:GO | Система заявок")
    await interaction.channel.send(embed=embed, view=ApplicationView())


@bot.tree.command(name="say", description="Отправить сообщение от имени бота")
@app_commands.checks.has_permissions(administrator=True)
async def say(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message.replace("\\n", "\n"))
    await interaction.response.send_message(f"Отправлено в {channel.mention}!", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
