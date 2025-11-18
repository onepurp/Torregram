
import asyncio
import math
import os
import libtorrent as lt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

import config
from state import AppState

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Send me a .torrent file to start.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send a .torrent file. I will show you the contents, and you can choose what to download and upload.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text("Invalid input. Please send a .torrent file.")

async def handle_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, session):
    file = await context.bot.get_file(update.message.document.file_id)
    file_path = os.path.join("temp", f"temp_{update.message.document.file_id}.torrent")
    await file.download_to_drive(file_path)
    await process_torrent_file(update, context, app_state, session, file_path)

async def process_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, session, file_path: str):
    try:
        info = await asyncio.to_thread(lt.torrent_info, file_path)
        info_hash_str = str(info.info_hashes().v1)
        
        if info_hash_str not in app_state.torrent_metadata_cache:
            app_state.torrent_metadata_cache[info_hash_str] = file_path
        
        params = {'ti': info, 'save_path': './downloads/'}
        handle = session.add_torrent(params)
        
        for tracker in config.PUBLIC_TRACKERS:
            handle.add_tracker({'url': tracker})
        
        handle.pause()
        handle.unset_flags(lt.torrent_flags.auto_managed)
        
        if info_hash_str not in app_state.active_torrents:
            app_state.active_torrents[info_hash_str] = {
                "handle": handle, "files_to_download": {}, "download_complete_files": [], 
                "successfully_uploaded_files": [], "status_message_id": None, "user_chat_id": None,
                "jobs_total": 0, "jobs_completed": 0
            }
            app_state.torrent_locks[info_hash_str] = asyncio.Lock()
        
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str)
    except Exception as e:
        await update.message.reply_text(f"Error processing torrent file: {e}")

async def display_torrent_info(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, info: lt.torrent_info, handle: lt.torrent_handle, info_hash_str: str, page: int = 0):
    files = info.files()
    total_files, total_pages = info.num_files(), math.ceil(info.num_files() / config.FILES_PER_PAGE)
    start_index = page * config.FILES_PER_PAGE
    end_index = (page + 1) * config.FILES_PER_PAGE
    
    message = f"**Torrent Name:** `{info.name()}`\n\n**Files (Page {page + 1} of {total_pages}):**\n"
    keyboard = []
    
    for index in range(start_index, min(end_index, total_files)):
        file_path, file_size = files.file_path(index), files.file_size(index)
        filename = os.path.basename(file_path)
        file_size_mb = round(file_size / (1024 * 1024), 2)
        message += f"- `{filename}` ({file_size_mb} MB)\n"
        
        if filename.lower().endswith(config.ARCHIVE_EXTENSIONS):
            button_text = f"🗂 Process {filename}"
            callback_data = f"archive_{info_hash_str}_{index}"
        else:
            button_text = f"⬇️ Download {filename}"
            callback_data = f"select_{info_hash_str}_{index}_noextract"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    nav_buttons = []
    if page > 0: nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"page_{info_hash_str}_{page - 1}"))
    if page < total_pages - 1: nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{info_hash_str}_{page + 1}"))
    if nav_buttons: keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("Download All Files", callback_data=f"select_{info_hash_str}_all_noextract")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        if update.callback_query: await update.callback_query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")
        else: await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e): raise e

async def _handle_archive_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, info_hash_str: str, value: str):
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("📦 Extract & Upload Contents", callback_data=f"select_{info_hash_str}_{value}_extract")],
        [InlineKeyboardButton("📎 Upload Archive as File", callback_data=f"select_{info_hash_str}_{value}_noextract")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("How would you like to process this archive?", reply_markup=reply_markup)

async def _handle_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, info_hash_str: str, value: str):
    query = update.callback_query
    torrent_file_path = app_state.torrent_metadata_cache.get(info_hash_str)
    if not torrent_file_path:
        await query.edit_message_text(text="Error: Torrent metadata has expired."); return
    
    info = await asyncio.to_thread(lt.torrent_info, torrent_file_path)
    handle = app_state.active_torrents.get(info_hash_str, {}).get("handle")
    if not handle:
        await query.edit_message_text(text="Error: This torrent is not active."); return
    
    new_page = int(value)
    await display_torrent_info(update, context, app_state, info, handle, info_hash_str, page=new_page)

async def _handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, session, info_hash_str: str, value: str, extract: bool):
    query = update.callback_query
    torrent_file_path = app_state.torrent_metadata_cache.get(info_hash_str)
    if not torrent_file_path:
        await query.edit_message_text(text="Error: Torrent metadata has expired."); return

    info = await asyncio.to_thread(lt.torrent_info, torrent_file_path)
    files = info.files()
    
    files_to_queue, total_size, skipped_files = [], 0, []
    indices_to_check = list(range(files.num_files())) if value == "all" else [int(value)]

    if info_hash_str not in app_state.active_torrents:
        params = {'ti': info, 'save_path': './downloads/'}
        handle = session.add_torrent(params)
        for tracker in config.PUBLIC_TRACKERS: handle.add_tracker({'url': tracker})
        handle.pause()
        handle.unset_flags(lt.torrent_flags.auto_managed)
        app_state.active_torrents[info_hash_str] = {
            "handle": handle, "files_to_download": {}, "download_complete_files": [], 
            "successfully_uploaded_files": [], "status_message_id": None, "user_chat_id": None,
            "jobs_total": 0, "jobs_completed": 0
        }
        app_state.torrent_locks[info_hash_str] = asyncio.Lock()
    torrent_data = app_state.active_torrents[info_hash_str]

    for index in indices_to_check:
        filename = os.path.basename(files.file_path(index))
        filesize = files.file_size(index)
        
        # --- FIX: The robust pre-download duplicate check ---
        # We skip if the user is NOT extracting AND the file fingerprint is already in our index.
        # If the user wants to extract, we must download it to see the contents.
        if not extract and (filename, filesize) in app_state.channel_file_index:
            skipped_files.append(filename)
        else:
            files_to_queue.append(index)
            total_size += filesize
            torrent_data["files_to_download"][index] = {"extract": extract}

    response_message = ""
    if files_to_queue:
        torrent_data["jobs_total"] += len(files_to_queue)
        
        await app_state.download_queue.put({
            "info_hash": info_hash_str, "file_indices": files_to_queue, 
            "total_size": total_size, "chat_id": update.effective_chat.id
        })
        app_state.new_download_event.set()
        response_message += f"✅ Queued {len(files_to_queue)} file(s) ({total_size / (1024*1024):.2f} MB) for download.\n"

    if skipped_files:
        response_message += f"⏭️ Skipped {len(skipped_files)} file(s) already in channel."
    if not response_message:
        response_message = "All selected files are already in the channel."

    try:
        await query.edit_message_text(text=response_message)
    except BadRequest as e:
        if "Message is not modified" not in str(e): print(f"An error occurred in button_callback: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, session):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    action = data[0]

    if action == "page":
        await _handle_pagination(update, context, app_state, info_hash_str=data[1], value=data[2])
    elif action == "archive":
        await _handle_archive_prompt(update, context, info_hash_str=data[1], value=data[2])
    elif action == "select":
        extract = data[3] == "extract"
        await _handle_selection(update, context, app_state, session, info_hash_str=data[1], value=data[2], extract=extract)