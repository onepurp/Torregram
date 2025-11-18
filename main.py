
import asyncio
import os
from functools import partial

from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telethon import TelegramClient

import config
import bot_handlers
import torrent_client
from state import AppState
from download_manager import download_manager_worker
from telegram_uploader import uploader_worker, fetch_and_load_trackers, load_index_from_disk

async def error_handler(update, context):
    print(f"An exception was raised while handling an update: {context.error}")

async def main() -> None:
    """Run the bot and all background workers."""
    app_state = AppState()
    session = torrent_client.initialize_session()

    # --- FIX: Create dedicated, safe directories for runtime files ---
    sessions_dir = "sessions"
    temp_dir = "temp"
    transcode_dir = os.path.join("downloads", ".transcode_temp") # Safe location
    os.makedirs(sessions_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(transcode_dir, exist_ok=True) # Create the safe transcode directory
    
    session_path = os.path.join(sessions_dir, "bot_session")
    telethon_client = TelegramClient(session_path, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    
    await fetch_and_load_trackers()
    
    load_index_from_disk(app_state)

    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(60.0)
        .build()
    )

    application.add_error_handler(error_handler)

    torrent_handler_partial = partial(bot_handlers.handle_torrent_file, app_state=app_state, session=session)
    button_callback_partial = partial(bot_handlers.button_callback, app_state=app_state, session=session)

    application.add_handler(CommandHandler("start", bot_handlers.start_command))
    application.add_handler(CommandHandler("help", bot_handlers.help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_handlers.handle_message))
    application.add_handler(MessageHandler(filters.Document.FileExtension("torrent"), torrent_handler_partial))
    application.add_handler(CallbackQueryHandler(button_callback_partial))

    uploader_tasks = []
    try:
        await telethon_client.start(bot_token=config.TELEGRAM_BOT_TOKEN)
        print("Telethon client started.")
        
        await application.initialize()
        print("Bot application initialized.")

        manager_task = asyncio.create_task(download_manager_worker(application, app_state, session))
        for i in range(config.NUM_UPLOAD_WORKERS):
            task = asyncio.create_task(uploader_worker(application, telethon_client, app_state, session))
            uploader_tasks.append(task)
            print(f"Uploader worker {i+1}/{config.NUM_UPLOAD_WORKERS} started.")
        
        await application.start()
        await application.updater.start_polling(poll_interval=1.0, timeout=30)
        print("Bot is polling for updates...")
        
        await asyncio.Event().wait()

    except (KeyboardInterrupt, SystemExit):
        print("Bot shutting down...")
    except Exception as e:
        print(f"An unexpected error occurred in main: {e}")
    finally:
        if 'manager_task' in locals() and not manager_task.done():
            manager_task.cancel()
        for task in uploader_tasks:
            if not task.done():
                task.cancel()
        
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()
        
        if telethon_client.is_connected():
            await telethon_client.disconnect()
            
        print("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())