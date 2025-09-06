import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
import datetime
import os
import psycopg
from dotenv import load_dotenv
import zoneinfo

# --- Setup ---
load_dotenv()
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
REMINDER_CHANNEL_ID = int(os.getenv('REMINDER_CHANNEL_ID'))
TEST_MODE = os.getenv('TEST_MODE', 'false').lower() == 'true'

intents = discord.Intents.default()
intents.members = True 
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Timezone Setup ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# --- 「学習日」を判定する新しいヘルパー関数 ---
def get_study_date(current_time_jst):
    """
    JSTの現在時刻を基に「学習日」を計算します。
    午前0時から3時までの時間は、前日の日付として扱います。
    """
    if current_time_jst.hour < 3:
        return current_time_jst.date() - datetime.timedelta(days=1)
    else:
        return current_time_jst.date()

# --- Database Functions ---
def save_record(conn, user_id, user_name, category, recorded_at):
    """Saves the main learning record."""
    with conn.cursor() as cur:
        sql = "INSERT INTO learning_records (user_id, user_name, category, recorded_at) VALUES (%s, %s, %s, %s);"
        cur.execute(sql, (user_id, user_name, category, recorded_at))
        print(f"SUCCESS: Saved record for {user_name} in category {category}.")

def update_streak(conn, user_id, study_date):
    """Calculates and updates the user's streak, returning the new streak."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_streak, last_study_date FROM user_stats WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        new_streak = 1
        if result:
            last_streak, last_study_date = result
            days_diff = (study_date - last_study_date).days
            if days_diff == 1: new_streak = last_streak + 1
            elif days_diff == 0: new_streak = last_streak
        
        upsert_sql = """
            INSERT INTO user_stats (user_id, current_streak, last_study_date) VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET current_streak = EXCLUDED.current_streak, last_study_date = EXCLUDED.last_study_date;
        """
        cur.execute(upsert_sql, (user_id, new_streak, study_date))
        print(f"SUCCESS: Updated streak for user {user_id} to {new_streak}.")
        return new_streak

def add_riga_coins(conn, user_id, amount_to_add):
    """Adds Riga Coins to a user's balance and returns the new total."""
    with conn.cursor() as cur:
        sql = """
            UPDATE user_stats
            SET riga_coin_balance = riga_coin_balance + %s
            WHERE user_id = %s
            RETURNING riga_coin_balance;
        """
        cur.execute(sql, (amount_to_add, user_id))
        new_balance = cur.fetchone()[0]
        print(f"SUCCESS: Awarded {amount_to_add} Riga to user {user_id}. New balance: {new_balance}")
        return new_balance

# --- Reminder Task ---
reminder_time = datetime.time(hour=22, minute=0, tzinfo=JST)
@tasks.loop(time=reminder_time)
async def check_for_reminders():
    # ... (このセクションは変更なし)
    await bot.wait_until_ready()
    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if not channel: return
    current_time_jst = datetime.datetime.now(JST)
    current_study_date = get_study_date(current_time_jst)
    previous_study_day = current_study_date - datetime.timedelta(days=1)
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                sql = "SELECT user_id FROM user_stats WHERE last_study_date = %s"
                cur.execute(sql, (previous_study_day,))
                users_to_remind_ids = [row[0] for row in cur.fetchall()]
                if not users_to_remind_ids: return
                for user_id in users_to_remind_ids:
                    try:
                        user = await bot.fetch_user(user_id)
                        await channel.send(f"{user.mention} まもなくお休みのお時間ですが、本日の学習記録がまだのようでございます。")
                    except discord.NotFound:
                        print(f"ERROR: User with ID {user_id} could not be found during reminder.")
                print(f"Sent reminders to {len(users_to_remind_ids)} users.")
    except (Exception, psycopg.DatabaseError) as error:
        print(f"ERROR during reminder task: {error}")

# --- Test Loop ---
@tasks.loop(minutes=5)
async def test_loop():
    # ... (このセクションは変更なし)
    if not TEST_MODE: return
    await bot.wait_until_ready()
    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if channel:
        await channel.send(f"Bot is running. This is a test message. ({datetime.datetime.now(JST).strftime('%H:%M')})")
        print("Sent a test message.")

# --- Discord UI Classes ---
class StudyCategoryView(View):
    def __init__(self):
        super().__init__(timeout=None)
        categories = ["単語帳", "教科書", "シャドウイング", "動画視聴", "学習アプリ", "オンライン英会話"]
        for category in categories: self.add_item(StudyButton(label=category))

class StudyButton(Button):
    def __init__(self, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"study_{label}")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id, user_name, button_label = interaction.user.id, interaction.user.name, self.label
        
        current_time_utc = datetime.datetime.now(datetime.timezone.utc)
        current_time_jst = current_time_utc.astimezone(JST)
        study_date = get_study_date(current_time_jst)
        
        is_first_record_of_day = False

        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT last_study_date FROM user_stats WHERE user_id = %s", (user_id,))
                    result = cur.fetchone()
                    if not result or (study_date - result[0]).days >= 1:
                        is_first_record_of_day = True

                save_record(conn, user_id, user_name, button_label, current_time_utc)
                new_streak = update_streak(conn, user_id, study_date)

                riga_awarded = 0
                new_balance = 0
                riga_message_addon = ""
                
                if new_streak >= 7:
                    if is_first_record_of_day:
                        potential_riga = new_streak - 6
                        riga_to_add = min(potential_riga, 50) 
                        new_balance = add_riga_coins(conn, user_id, riga_to_add)
                        riga_awarded = riga_to_add
                    else:
                        riga_to_add = 1
                        new_balance = add_riga_coins(conn, user_id, riga_to_add)
                        riga_awarded = riga_to_add
                        riga_message_addon = "\n本日2回目以降の記録のため、獲得 Riga は **1 Riga** となりました。"

                message = f"{interaction.user.mention} さんが **{button_label}** の学習を記録しました。お見事です！"
                if new_streak > 1:
                    message += f"\n\n**🔥 これで{new_streak}日連続です！**"
                
                if riga_awarded > 0:
                    message += f"\n**{riga_awarded} Riga** を新たに獲得し、合計保有額は **{new_balance} Riga** となりました。"
                    message += riga_message_addon
                
            await interaction.followup.send(message)

        except (Exception, psycopg.DatabaseError) as error:
            print(f"ERROR: {error}")
            await interaction.followup.send("エラーが発生しました。記録に失敗した可能性があります。", ephemeral=True)
            
# --- Discord Bot Slash Commands ---
@bot.tree.command(name="study", description="学習内容を記録します。")
async def study(interaction: discord.Interaction):
    await interaction.response.send_message("記録するカテゴリを選択してください。", view=StudyCategoryView(), ephemeral=True)

@bot.tree.command(name="ranking", description="学習回数ランキングを表示します。")
async def ranking(interaction: discord.Interaction):
    # ... (このセクションは変更なし)
    await interaction.response.defer()
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                sql = """
                    SELECT user_name, COUNT(id) as study_count
                    FROM learning_records WHERE recorded_at >= NOW() - INTERVAL '7 days'
                    GROUP BY user_name ORDER BY study_count DESC LIMIT 10;
                """
                cur.execute(sql)
                results = cur.fetchall()
                embed = discord.Embed(title="🏆 週間学習ランキング 🏆", description="過去7日間の学習記録回数のトップ10です！", color=discord.Color.gold())
                if not results:
                    embed.description = "まだ過去7日間の学習記録がありません。"
                    await interaction.followup.send(embed=embed)
                    return
                rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
                for i, (user_name, study_count) in enumerate(results, 1):
                    rank_display = rank_emojis.get(i, f"**{i}.**")
                    embed.add_field(name=f"{rank_display} {user_name} さん", value=f"`{study_count} 回`", inline=False)
                await interaction.followup.send(embed=embed)
    except (Exception, psycopg.DatabaseError) as error:
        print(f"ERROR during ranking command: {error}")
        await interaction.followup.send("ランキングの取得中にエラーが発生しました。", ephemeral=True)

# <--- 変更点: /balanceコマンドを追加 ---
@bot.tree.command(name="balance", description="Riga Coinの保有残高ランキングを表示します。")
async def balance(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                sql = """
                    SELECT user_id, riga_coin_balance
                    FROM user_stats
                    WHERE riga_coin_balance > 0
                    ORDER BY riga_coin_balance DESC;
                """
                cur.execute(sql)
                results = cur.fetchall()

                embed = discord.Embed(
                    title="💰 Riga Coin 保有残高 💰",
                    color=discord.Color.yellow()
                )

                if not results:
                    embed.description = "まだ誰もRiga Coinを保有していません。"
                    await interaction.followup.send(embed=embed)
                    return

                description_text = ""
                for i, (user_id, coin_balance) in enumerate(results, 1):
                    try:
                        user = await bot.fetch_user(user_id)
                        description_text += f"**{i}.** {user.mention} : **{coin_balance}** Riga\n"
                    except discord.NotFound:
                        # ユーザーが見つからない場合はスキップ
                        continue
                
                embed.description = description_text
                await interaction.followup.send(embed=embed)

    except (Exception, psycopg.DatabaseError) as error:
        print(f"ERROR during balance command: {error}")
        await interaction.followup.send("残高の取得中にエラーが発生しました。", ephemeral=True)

# <--- 変更点: /sendコマンドを追加 ---
@bot.tree.command(name="send", description="他のユーザーにRiga Coinを送信します。")
@discord.app_commands.describe(
    recipient="送信先のユーザー",
    amount="送信するRigaの額"
)
async def send(interaction: discord.Interaction, recipient: discord.Member, amount: int):
    sender_id = interaction.user.id
    recipient_id = recipient.id

    if sender_id == recipient_id:
        await interaction.response.send_message("自分自身にRiga Coinを送信することはできません。", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("1以上の値を指定してください。", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            # トランザクション内で処理を実行
            with conn.transaction():
                with conn.cursor() as cur:
                    # 1. 送信者の残高を確認
                    cur.execute("SELECT riga_coin_balance FROM user_stats WHERE user_id = %s", (sender_id,))
                    sender_balance = cur.fetchone()
                    if not sender_balance or sender_balance[0] < amount:
                        await interaction.followup.send("Riga Coinの残高が不足しています。", ephemeral=True)
                        return # ここで処理を中断するとトランザクションは自動的にロールバックされる

                    # 2. 送信者の残高を減らす
                    cur.execute("UPDATE user_stats SET riga_coin_balance = riga_coin_balance - %s WHERE user_id = %s", (amount, sender_id))

                    # 3. 受信者の残高を増やす（存在しない場合は新しい記録を作成）
                    upsert_sql = """
                        INSERT INTO user_stats (user_id, current_streak, last_study_date, riga_coin_balance)
                        VALUES (%s, 0, '1970-01-01', %s)
                        ON CONFLICT (user_id) DO UPDATE
                        SET riga_coin_balance = user_stats.riga_coin_balance + %s;
                    """
                    cur.execute(upsert_sql, (recipient_id, amount, amount))

        # トランザクションが成功した場合のみ、このメッセージが送信される
        await interaction.followup.send(f"{interaction.user.mention} さんから {recipient.mention} さんへ **{amount} Riga** が送信されました。")

    except (Exception, psycopg.DatabaseError) as error:
        print(f"ERROR during send command: {error}")
        await interaction.followup.send("送信処理中にエラーが発生しました。", ephemeral=True)

# --- Bot Startup ---
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_for_reminders.is_running(): check_for_reminders.start()
    if not test_loop.is_running(): test_loop.start()
    
    print(f'{bot.user} としてログインしました')
    print('------')

bot.run(BOT_TOKEN)

