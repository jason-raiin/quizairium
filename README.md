# QuizAIrium Bot

QuizAIrium is an AI-powered Telegram bot that runs trivia games in group chats. 

## Usage

To play, add [`@QuizAIriumBot`](https://t.me/QuizairiumBot) to your Telegram group chat and start a new game with `/start`.

## Development

Clone the repository:
```bash
git clone https://github.com/jason-raiin/quizairium
```

Install the required dependencies:

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root with the following variables:

```bash
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
MONGODB_URI=YOUR_MONGODB_URI
```

Run the bot:

```bash
python main.py 
```