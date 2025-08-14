import os
import asyncio
import json
import time
from datetime import datetime
from typing import Dict, List, Optional
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import openai
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class TriviaBot:
    def __init__(self, telegram_token: str, openai_api_key: str, mongodb_uri: str):
        self.telegram_token = telegram_token
        self.openai_client = openai.AsyncOpenAI(api_key=openai_api_key)
        
        # MongoDB setup
        self.mongo_client = MongoClient(mongodb_uri)
        self.db = self.mongo_client.trivia_bot
        self.games_collection = self.db.games
        self.scores_collection = self.db.scores
        
        # Active games storage
        self.active_games: Dict[int, Dict] = {}
        
        # Categories for trivia
        self.categories = {
            "general": "General Knowledge",
            "science": "Science & Technology", 
            "history": "History",
            "geography": "Geography",
            "sports": "Sports",
            "entertainment": "Movies & TV",
            "music": "Music",
            "literature": "Literature"
        }

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        chat_id = update.effective_chat.id
        
        # Check if this is a group chat
        if update.effective_chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("This bot only works in group chats! Add me to a group to start playing trivia.")
            return
        
        # Check if there's already an active game
        if chat_id in self.active_games:
            await update.message.reply_text("There's already an active trivia game in this chat! Please wait for it to finish.")
            return
        
        # Show duration selection
        keyboard = [
            [InlineKeyboardButton("5 Questions", callback_data="duration_5")],
            [InlineKeyboardButton("10 Questions", callback_data="duration_10")],
            [InlineKeyboardButton("15 Questions", callback_data="duration_15")],
            [InlineKeyboardButton("20 Questions", callback_data="duration_20")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🎯 Welcome to Trivia Bot! 🎯\n\n"
            "Choose the number of questions for your trivia game:",
            reply_markup=reply_markup
        )

    async def duration_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle duration selection"""
        query = update.callback_query
        await query.answer()
        
        chat_id = query.message.chat_id
        duration = int(query.data.split("_")[1])
        
        # Store game initialization
        self.active_games[chat_id] = {
            "duration": duration,
            "started_by": query.from_user.id,
            "status": "selecting_category"
        }
        
        # Show category selection
        keyboard = []
        for key, value in self.categories.items():
            keyboard.append([InlineKeyboardButton(value, callback_data=f"category_{key}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"Great! You've chosen {duration} questions.\n\n"
            "Now select a category:",
            reply_markup=reply_markup
        )

    async def category_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle category selection and start the game"""
        query = update.callback_query
        await query.answer()
        
        chat_id = query.message.chat_id
        category = query.data.split("_")[1]
        
        if chat_id not in self.active_games:
            await query.edit_message_text("Error: No active game found. Please start a new game with /start")
            return
        
        # Initialize game
        game_data = {
            "chat_id": chat_id,
            "duration": self.active_games[chat_id]["duration"],
            "category": category,
            "category_name": self.categories[category],
            "current_question": 0,
            "questions": [],
            "scores": {},
            "status": "active",
            "created_at": datetime.now(),
            "started_by": self.active_games[chat_id]["started_by"]
        }
        
        # Save to MongoDB
        result = self.games_collection.insert_one(game_data)
        game_id = result.inserted_id
        
        # Update active games
        self.active_games[chat_id].update({
            "game_id": game_id,
            "category": category,
            "category_name": self.categories[category],
            "current_question": 0,
            "questions": [],
            "scores": {},
            "status": "active"
        })
        
        await query.edit_message_text(
            f"🎮 Starting trivia game!\n"
            f"📊 Questions: {game_data['duration']}\n"
            f"📚 Category: {self.categories[category]}\n\n"
            f"Get ready! First question coming up..."
        )
        
        # Start the first question
        await self.next_question(chat_id, context)

    async def generate_question(self, category: str) -> Dict:
        """Generate a trivia question using OpenAI"""
        category_name = self.categories[category]
        
        prompt = f"""Generate a trivia question in the {category_name} category. 
        
        Return a JSON object with exactly this structure:
        {{
            "question": "The trivia question here",
            "official_answer": "The main correct answer",
            "acceptable_answers": ["answer1", "answer2", "answer3"]
        }}
        
        The acceptable_answers should include the official answer plus alternative ways to express the same answer (different spellings, abbreviations, etc.). Make sure all answers are lowercase for easier matching.
        
        Make the question challenging but fair, suitable for a group trivia game."""
        
        try:
            response = await self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a trivia question generator. Always respond with valid JSON only."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=300,
                temperature=0.7
            )
            
            content = response.choices[0].message.content.strip()
            question_data = json.loads(content)
            
            # Ensure acceptable_answers is lowercase
            question_data["acceptable_answers"] = [ans.lower().strip() for ans in question_data["acceptable_answers"]]
            
            return question_data
            
        except Exception as e:
            logger.error(f"Error generating question: {e}")
            # Fallback question
            return {
                "question": "What is the capital of France?",
                "official_answer": "Paris",
                "acceptable_answers": ["paris"]
            }

    async def next_question(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        """Send the next question"""
        if chat_id not in self.active_games:
            return
        
        game = self.active_games[chat_id]
        
        # Check if game is finished
        if game["current_question"] >= game["duration"]:
            await self.end_game(chat_id, context)
            return
        
        # Generate new question
        question_data = await self.generate_question(game["category"])
        
        # Store question data
        game["questions"].append(question_data)
        game["current_question"] += 1
        game["question_start_time"] = time.time()
        game["answered"] = False
        
        # Send question
        question_text = (
            f"❓ **Question {game['current_question']}/{game['duration']}**\n\n"
            f"{question_data['question']}\n\n"
            f"⏱️ You have 30 seconds to answer!"
        )
        
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=question_text,
            parse_mode='Markdown'
        )
        
        # Store message ID for potential deletion
        game["current_message_id"] = message.message_id
        
        # Set timer for 30 seconds
        context.job_queue.run_once(
            self.question_timeout,
            30,
            data={"chat_id": chat_id},
            name=f"timeout_{chat_id}_{game['current_question']}"
        )

    async def question_timeout(self, context: ContextTypes.DEFAULT_TYPE):
        """Handle question timeout"""
        chat_id = context.job.data["chat_id"]
        
        if chat_id not in self.active_games:
            return
        
        game = self.active_games[chat_id]
        
        if game["answered"]:
            return  # Question was already answered
        
        # Show correct answer
        current_q = game["questions"][-1]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Time's up! The correct answer was: **{current_q['official_answer']}**",
            parse_mode='Markdown'
        )
        
        # Move to next question after a short delay
        await asyncio.sleep(2)
        await self.next_question(chat_id, context)

    async def check_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check if a message contains a correct answer"""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        user_answer = update.message.text.lower().strip()
        
        if chat_id not in self.active_games:
            return
        
        game = self.active_games[chat_id]
        
        if game["answered"] or not game.get("question_start_time"):
            return
        
        # Get current question
        current_q = game["questions"][-1]
        
        # Check if answer is correct
        if user_answer in current_q["acceptable_answers"]:
            # Calculate score
            time_elapsed = time.time() - game["question_start_time"]
            time_remaining = max(0, 30 - time_elapsed)
            points = int(time_remaining / 6) if time_remaining > 0 else 0
            
            # Update scores
            if user_id not in game["scores"]:
                game["scores"][user_id] = {"username": username, "points": 0}
            game["scores"][user_id]["points"] += points
            
            # Mark as answered
            game["answered"] = True
            
            # Cancel timeout job
            current_jobs = context.job_queue.get_jobs_by_name(f"timeout_{chat_id}_{game['current_question']}")
            for job in current_jobs:
                job.schedule_removal()
            
            # Send success message
            await update.message.reply_text(
                f"🎉 Correct! **{username}** got it right!\n"
                f"Answer: {current_q['official_answer']}\n"
                f"Points earned: {points} pts (+{time_remaining:.1f}s remaining)",
                parse_mode='Markdown'
            )
            
            # Move to next question after a short delay
            await asyncio.sleep(3)
            await self.next_question(chat_id, context)

    async def end_game(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        """End the game and show leaderboard"""
        if chat_id not in self.active_games:
            return
        
        game = self.active_games[chat_id]
        
        # Update game in database
        self.games_collection.update_one(
            {"_id": game["game_id"]},
            {
                "$set": {
                    "scores": game["scores"],
                    "status": "completed",
                    "completed_at": datetime.utcnow()
                }
            }
        )
        
        # Save individual scores to scores collection
        for user_id, score_data in game["scores"].items():
            self.scores_collection.update_one(
                {"user_id": user_id, "chat_id": chat_id},
                {
                    "$inc": {"total_points": score_data["points"], "games_played": 1},
                    "$set": {"username": score_data["username"], "last_played": datetime.utcnow()}
                },
                upsert=True
            )
        
        # Create leaderboard
        if game["scores"]:
            sorted_scores = sorted(
                game["scores"].items(),
                key=lambda x: x[1]["points"],
                reverse=True
            )
            
            leaderboard = "🏆 **FINAL LEADERBOARD** 🏆\n\n"
            medals = ["🥇", "🥈", "🥉"]
            
            for i, (user_id, score_data) in enumerate(sorted_scores):
                medal = medals[i] if i < 3 else f"{i+1}."
                leaderboard += f"{medal} **{score_data['username']}** - {score_data['points']} pts\n"
        else:
            leaderboard = "🤷‍♂️ No one scored any points this round! Better luck next time!"
        
        leaderboard += f"\n🎮 Game completed! Thanks for playing!\nUse /start to play again."
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=leaderboard,
            parse_mode='Markdown'
        )
        
        # Clean up
        del self.active_games[chat_id]

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show player statistics"""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Get user stats
        user_stats = self.scores_collection.find_one({"user_id": user_id, "chat_id": chat_id})
        
        if not user_stats:
            await update.message.reply_text("You haven't played any games yet! Use /start to begin.")
            return
        
        stats_text = (
            f"📊 **Your Stats** 📊\n\n"
            f"🎮 Games played: {user_stats.get('games_played', 0)}\n"
            f"🎯 Total points: {user_stats.get('total_points', 0)}\n"
            f"📈 Average per game: {user_stats.get('total_points', 0) / max(user_stats.get('games_played', 1), 1):.1f}\n"
            f"🕒 Last played: {user_stats.get('last_played', 'Never').strftime('%Y-%m-%d') if user_stats.get('last_played') else 'Never'}"
        )
        
        await update.message.reply_text(stats_text, parse_mode='Markdown')

    async def leaderboard_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show chat leaderboard"""
        chat_id = update.effective_chat.id
        
        # Get top players in this chat
        top_players = list(self.scores_collection.find(
            {"chat_id": chat_id}
        ).sort("total_points", -1).limit(10))
        
        if not top_players:
            await update.message.reply_text("No games have been played in this chat yet!")
            return
        
        leaderboard = "🏆 **CHAT LEADERBOARD** 🏆\n\n"
        medals = ["🥇", "🥈", "🥉"]
        
        for i, player in enumerate(top_players):
            medal = medals[i] if i < 3 else f"{i+1}."
            leaderboard += f"{medal} **{player['username']}** - {player['total_points']} pts ({player['games_played']} games)\n"
        
        await update.message.reply_text(leaderboard, parse_mode='Markdown')

    def run(self):
        """Run the bot"""
        application = Application.builder().token(self.telegram_token).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(CommandHandler("leaderboard", self.leaderboard_command))
        application.add_handler(CallbackQueryHandler(self.duration_callback, pattern="^duration_"))
        application.add_handler(CallbackQueryHandler(self.category_callback, pattern="^category_"))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.check_answer))
        
        # Start the bot
        application.run_polling()

# Main execution
if __name__ == "__main__":
    # Environment variables
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") 
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY environment variable is required")
    
    # Create and run bot
    bot = TriviaBot(TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, MONGODB_URI)
    bot.run()