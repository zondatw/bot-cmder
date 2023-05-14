set dotenv-load := true
set positional-arguments := true

@show-env-settings:
    echo "--- .env settings ---"
    echo "TELEGRAM_TOKEN: $TELEGRAM_TOKEN"
    echo "TELEGRAM_HOOK_URL: $TELEGRAM_HOOK_URL"

@set-telegram-bot-webhook:
    curl -v "https://api.telegram.org/bot$TELEGRAM_TOKEN/setWebhook?url=$TELEGRAM_HOOK_URL"

@get-updates-from-telegram-bot:
    curl -v "https://api.telegram.org/bot$TELEGRAM_TOKEN/getUpdates"

@send-text-to-user-from-telegram-bot user_id text:
    # curl -v "https://api.telegram.org/bot$TELEGRAM_TOKEN/sendMessage?chat_id=$1&text=$2"
