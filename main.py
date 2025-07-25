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

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Database Functions ---
def save_record(conn, user_id, user_name, category, recorded_at):
    with conn.cursor() as cur:
        sql = "INSERT INTO learning_records (user_id, user_name, category, recorded_at) VALUES (%s, %s, %s, %s);"
        cur.execute(sql, (user_id, user_name, category, recorded_at))
        print(f"SUCCESS: Saved record for {user_name} in category {category}.")

def update_streak(conn, user_id, today_date):
    with conn.cursor() as cur:
        cur.execute("SELECT current_streak, last_study_date FROM user_stats WHERE user_id = %s", (user_id,))
        result = cur.fetchone()
        new_streak = 1
        if result:
            last_streak, last_study_date = result
            days_diff = (today_date - last_study_date).days
            if days_diff == 1: new_streak = last_streak + 1
            elif days_diff == 0: new_streak = last_streak
        upsert_sql = """
            INSERT INTO user_stats (user_id, current_streak, last_study_date) VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET current_streak = EXCLUDED.current_streak, last_study_date = EXCLUDED.last_study_date;
        """
        cur.execute(upsert_sql, (user_id, new_streak, today_date))
        print(f"SUCCESS: Updated streak for user {user_id} to {new_streak}.")
        return new_streak

# --- Reminder Task ---
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
reminder_time = datetime.time(hour=22, minute=0, tzinfo=JST)

@tasks.loop(time=reminder_time)
async def check_for_reminders():
    await bot.wait_until_ready()
    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if not channel:
        print(f"ERROR: Reminder channel with ID {REMINDER_CHANNEL_ID} not found.")
        return

    today = datetime.datetime.now(JST).date()
    yesterday = today - datetime.timedelta(days=1)
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                sql = "SELECT user_id FROM user_stats WHERE last_study_date = %s"
                cur.execute(sql, (yesterday,))
                users_to_remind_ids = [row[0] for row in cur.fetchall()]
                if not users_to_remind_ids:
                    print("No users to remind today.")
                    return
                for user_id in users_to_remind_ids:
                    user = bot.get_user(user_id)
                    if user: await channel.send(f"{user.mention} まもなくお休みのお時間ですが、本日の学習記録がまだのようでございます。")
                print(f"Sent reminders to {len(users_to_remind_ids)} users.")
    except (Exception, psycopg.DatabaseError) as error:
        print(f"ERROR during reminder task: {error}")

# --- Discord UI Classes ---
class StudyCategoryView(View):
    def __init__(self):
        super().__init__(timeout=None)
        categories = ["単語帳", "教科書", "シャドウイング", "動画視聴", "学習アプリ"]
        for category in categories: self.add_item(StudyButton(label=category))

class StudyButton(Button):
    def __init__(self, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"study_{label}")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id, user_name, button_label = interaction.user.id, interaction.user.name, self.label
        current_time, today_date = datetime.datetime.now(datetime.timezone.utc), datetime.datetime.now(datetime.timezone.utc).date()
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                save_record(conn, user_id, user_name, button_label, current_time)
                new_streak = update_streak(conn, user_id, today_date)
            message = f"{interaction.user.mention} さんが **{button_label}** の学習を記録しました。お見事です！"
            if new_streak > 1: message += f"\n\n**🔥 これで{new_streak}日連続です！**"
            await interaction.followup.send(message)
        except (Exception, psycopg.DatabaseError) as error:
            print(f"ERROR: {error}")
            await interaction.followup.send("エラーが発生しました。記録に失敗した可能性があります。", ephemeral=True)

# --- Discord Bot Slash Commands ---
@bot.tree.command(name="study", description="学習内容を記録します。")
async def study(interaction: discord.Interaction):
    await interaction.response.send_message("記録するカテゴリを選択してください。", view=StudyCategoryView(), ephemeral=True)

## --- NEW RANKING COMMAND ---
@bot.tree.command(name="ranking", description="週間学習回数ランキングを表示します。")
async def ranking(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # SQL to get top 10 users by study count in the last 30 days
                sql = """
                    SELECT user_name, COUNT(id) as study_count
                    FROM learning_records
                    WHERE recorded_at >= NOW() - INTERVAL '30 days'
                    GROUP BY user_name
                    ORDER BY study_count DESC
                    LIMIT 10;
                """
                cur.execute(sql)
                results = cur.fetchall()

                embed = discord.Embed(
                    title="🏆 月間学習ランキング 🏆",
                    description="過去30日間の学習記録回数のトップ10です！",
                    color=discord.Color.gold()
                )

                if not results:
                    embed.description = "まだ過去30日間の学習記録がありません。"
                    await interaction.followup.send(embed=embed)
                    return

                rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
                for i, (user_name, study_count) in enumerate(results, 1):
                    rank_display = rank_emojis.get(i, f"**{i}.**")
                    embed.add_field(
                        name=f"{rank_display} {user_name} さん",
                        value=f"`{study_count} 回`",
                        inline=False
                    )
                
                await interaction.followup.send(embed=embed)

    except (Exception, psycopg.DatabaseError) as error:
        print(f"ERROR during ranking command: {error}")
        await interaction.followup.send("ランキングの取得中にエラーが発生しました。", ephemeral=True)


@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_for_reminders.is_running():
        check_for_reminders.start()
    print(f'{bot.user} としてログインしました')
    print('------')

bot.run(BOT_TOKEN)