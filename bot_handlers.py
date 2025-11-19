import asyncio
import math
import os
import libtorrent as lt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

import config
from state import AppState
from telegram_uploader import refresh_status_panel

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
                "handle": handle, 
                "files_to_download": {}, 
                "download_complete_files": [], 
                "successfully_uploaded_files": [], 
                "status_message_id": None, 
                "user_chat_id": None,
                "jobs_total": 0, 
                "jobs_completed": 0, 
                "seeding_paused": False,
                "details_visible": False, 
                "selection_mode": False, 
                "selection": set(),
                # --- NEW: Sequencing State ---
                "upload_order": [],       # List of file indices in the correct order
                "current_upload_idx": 0,  # Pointer to the current index in upload_order
                "ready_buffer": {}        # Storage for processed files waiting for their turn
            }
            app_state.torrent_locks[info_hash_str] = asyncio.Lock()
        
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str)
    except Exception as e:
        await update.message.reply_text(f"Error processing torrent file: {e}")

async def display_torrent_info(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, info: lt.torrent_info, handle: lt.torrent_handle, info_hash_str: str, page: int = 0):
    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data: return

    selection_mode = torrent_data["selection_mode"]
    selection = torrent_data["selection"]
    files = info.files()
    total_files, total_pages = info.num_files(), math.ceil(info.num_files() / config.FILES_PER_PAGE)
    start_index = page * config.FILES_PER_PAGE
    end_index = (page + 1) * config.FILES_PER_PAGE
    
    message = f"**Torrent Name:** `{info.name()}`\n\n"
    if selection_mode:
        message += f"**-- Selection Mode (Page {page + 1}/{total_pages}) --**\n"
    else:
        message += f"**Files (Page {page + 1}/{total_pages}):**\n"
    
    keyboard = []
    
    for index in range(start_index, min(end_index, total_files)):
        file_path, file_size = files.file_path(index), files.file_size(index)
        filename = os.path.basename(file_path)
        file_size_mb = round(file_size / (1024 * 1024), 2)
        
        if selection_mode:
            is_selected = index in selection
            prefix = "✅" if is_selected else "🔲"
            message += f"{prefix} `{filename}` ({file_size_mb} MB)\n"
            button_text = "➖ Remove" if is_selected else "➕ Add"
            callback_data = f"removeselect_{info_hash_str}_{index}_{page}" if is_selected else f"addselect_{info_hash_str}_{index}_{page}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        else:
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
    
    if selection_mode:
        action_buttons = []
        if selection:
            action_buttons.append(InlineKeyboardButton("⬇️ Download Selected", callback_data=f"applyselect_{info_hash_str}_noextract"))
            action_buttons.append(InlineKeyboardButton("📦 Extract Selected", callback_data=f"applyselect_{info_hash_str}_extract"))
        keyboard.append(action_buttons)
        keyboard.append([
            InlineKeyboardButton("🔙 Back to List", callback_data=f"exitselect_{info_hash_str}_{page}"),
            InlineKeyboardButton("🗑️ Clear Selection", callback_data=f"clearselect_{info_hash_str}_{page}")
        ])
    else:
        keyboard.append([InlineKeyboardButton("✏️ Select Multiple...", callback_data=f"enterselect_{info_hash_str}_{page}")])
        keyboard.append([InlineKeyboardButton("📥 Process All Files...", callback_data=f"processall_{info_hash_str}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        if update.callback_query: await update.callback_query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")
        else: await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e): raise e

async def _handle_process_all_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, info_hash_str: str):
    query = update.callback_query
    message = (
        "**Process All Files**\n\n"
        "How would you like to handle the entire torrent?"
    )
    keyboard = [
        [InlineKeyboardButton("📎 Download All As-Is", callback_data=f"select_{info_hash_str}_all_noextract")],
        [InlineKeyboardButton("📦 Smart Extract All", callback_data=f"select_{info_hash_str}_all_extract")],
        [InlineKeyboardButton("🔙 Back to File List", callback_data=f"page_{info_hash_str}_0")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(message, reply_markup=reply_markup, parse_mode="Markdown")

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

async def _handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, session, info_hash_str: str, indices: list, extract: bool):
    query = update.callback_query
    torrent_file_path = app_state.torrent_metadata_cache.get(info_hash_str)
    if not torrent_file_path:
        await query.edit_message_text(text="Error: Torrent metadata has expired."); return

    info = await asyncio.to_thread(lt.torrent_info, torrent_file_path)
    files = info.files()
    
    files_to_queue, total_size, skipped_files = [], 0, []
    
    torrent_data = app_state.active_torrents[info_hash_str]

    # Sort indices to ensure we add them to the order list correctly
    indices.sort()

    for index in indices:
        filename = os.path.basename(files.file_path(index))
        filesize = files.file_size(index)
        
        is_archive = filename.lower().endswith(config.ARCHIVE_EXTENSIONS)
        should_extract_this_file = extract and is_archive

        if not should_extract_this_file and (filename, filesize) in app_state.channel_file_index:
            skipped_files.append(filename)
        else:
            files_to_queue.append(index)
            total_size += filesize
            torrent_data["files_to_download"][index] = {"extract": should_extract_this_file}
            # --- FIX: Add to the ordered list of uploads ---
            torrent_data["upload_order"].append(index)

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

async def _handle_cancellation(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, session, info_hash_str: str):
    query = update.callback_query
    lock = app_state.torrent_locks.get(info_hash_str)
    if not lock:
        await query.edit_message_text("This torrent has already been completed or cancelled.")
        return

    async with lock:
        torrent_data = app_state.active_torrents.get(info_hash_str)
        if not torrent_data:
            await query.edit_message_text("This torrent has already been completed or cancelled.")
            return

        print(f"Cancelling torrent: {info_hash_str}")
        handle = torrent_data["handle"]

        jobs = context.job_queue.get_jobs_by_name(f"job_{info_hash_str}")
        for job in jobs:
            job.schedule_removal()
            print(f"Removed job: {job.name}")

        if handle.is_valid():
            await asyncio.to_thread(session.remove_torrent, handle, session.delete_files)

        if info_hash_str in app_state.torrent_metadata_cache:
            temp_torrent_path = app_state.torrent_metadata_cache.pop(info_hash_str)
            if os.path.exists(temp_torrent_path):
                await asyncio.to_thread(os.remove, temp_torrent_path)
        
        if info_hash_str in app_state.active_torrents:
            del app_state.active_torrents[info_hash_str]
        if info_hash_str in app_state.torrent_locks:
            del app_state.torrent_locks[info_hash_str]

    await query.edit_message_text("✅ **Cancelled:** The torrent has been stopped and all associated files have been deleted.")
    print(f"Successfully cancelled and cleaned up torrent: {info_hash_str}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, app_state: AppState, session):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    action = data[0]
    info_hash_str = data[1]

    torrent_data = app_state.active_torrents.get(info_hash_str)
    if not torrent_data and action not in ["page", "exitselect"]:
        await query.edit_message_text("This torrent is no longer active.")
        return
    
    info = await asyncio.to_thread(lt.torrent_info, app_state.torrent_metadata_cache[info_hash_str])
    handle = torrent_data["handle"] if torrent_data else None
    
    page = 0
    if len(data) > 2:
        try:
            page = int(data[-1])
        except ValueError:
            pass

    if action == "page":
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str, page=int(data[2]))
    elif action == "archive":
        await _handle_archive_prompt(update, context, info_hash_str, value=data[2])
    elif action == "select":
        extract = data[3] == "extract"
        indices = list(range(info.num_files())) if data[2] == "all" else [int(data[2])]
        await _handle_selection(update, context, app_state, session, info_hash_str, indices, extract)
    elif action == "details":
        torrent_data["details_visible"] = not torrent_data["details_visible"]
        await refresh_status_panel(context.bot, app_state, info_hash_str, "Toggled details view")
    elif action == "cancel":
        await _handle_cancellation(update, context, app_state, session, info_hash_str)
    elif action == "processall":
        await _handle_process_all_prompt(update, context, info_hash_str)
    
    elif action == "enterselect":
        torrent_data["selection_mode"] = True
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str, page=page)
    elif action == "exitselect":
        torrent_data["selection_mode"] = False
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str, page=page)
    elif action == "addselect":
        torrent_data["selection"].add(int(data[2]))
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str, page=page)
    elif action == "removeselect":
        torrent_data["selection"].discard(int(data[2]))
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str, page=page)
    elif action == "clearselect":
        torrent_data["selection"].clear()
        await display_torrent_info(update, context, app_state, info, handle, info_hash_str, page=page)
    elif action == "applyselect":
        extract = data[2] == "extract"
        indices = list(torrent_data["selection"])
        if not indices:
            await query.answer("No files selected!", show_alert=True)
            return
        torrent_data["selection_mode"] = False
        torrent_data["selection"].clear()
        await _handle_selection(update, context, app_state, session, info_hash_str, indices, extract)
