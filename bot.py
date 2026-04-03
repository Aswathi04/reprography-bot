import os, uuid, asyncio
from dotenv import load_dotenv
from supabase import create_client
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    filters, ContextTypes
)

# Load environment variables
load_dotenv()
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_KEY']
BOT_TOKEN   = os.environ['BOT_TOKEN']
BUCKET      = 'print-files'

# Initialize Supabase
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# Conversation states
AWAIT_FILE, AWAIT_COLOUR, AWAIT_SIDES, AWAIT_COPIES, \
AWAIT_PAPER, AWAIT_NOTES, CONFIRM = range(7)

# ── Helpers ──────────────────────────────────────────────────
def next_order_number():
    res = sb.table('orders').select('order_number') \
            .order('created_at', desc=True).limit(1).execute()
    if not res.data:
        return 'REP-0001'
    last = int(res.data[0]['order_number'].split('-')[1])
    return f'REP-{last+1:04d}'

async def upload_file(file_bytes, filename):
    key = f'{uuid.uuid4()}/{filename}'
    sb.storage.from_(BUCKET).upload(
        path=key,
        file=file_bytes,
        file_options={'content-type': 'application/octet-stream'}
    )
    return key

# ── Command handlers ─────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Welcome to Reprography!\n\n'
        'Send /print to submit a new print job.\n'
        'Send /status to check your latest order.'
    )

async def print_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        'Please upload your file.\n'
        'Supported formats: PDF, DOCX, JPG, PNG (max 20 MB)'
    )
    return AWAIT_FILE

async def receive_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Logic to handle both Documents and Photos (Tuples)
    if update.message.document:
        doc = update.message.document
    elif update.message.photo:
        # Photos are sent as a tuple of sizes; pick the largest one
        doc = update.message.photo[-1]
    else:
        await update.message.reply_text('Please send a supported file or photo.')
        return AWAIT_FILE

    if doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text('File is too large (Max 20MB).')
        return AWAIT_FILE

    ctx.user_data['file_id']   = doc.file_id
    ctx.user_data['file_name'] = getattr(doc, 'file_name', 'photo.jpg')
    ctx.user_data['file_size'] = doc.file_size

    await update.message.reply_text(
        f'File received: {ctx.user_data["file_name"]}',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('Black & White', callback_data='bw'),
            InlineKeyboardButton('Colour', callback_data='colour'),
        ]])
    )
    return AWAIT_COLOUR

async def receive_colour(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data['colour_mode'] = update.callback_query.data
    await update.callback_query.message.reply_text(
        'Sides?',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('Single-sided', callback_data='single'),
            InlineKeyboardButton('Double-sided', callback_data='double'),
        ]])
    )
    return AWAIT_SIDES

async def receive_sides(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data['sides'] = update.callback_query.data
    await update.callback_query.message.reply_text('How many copies? (type a number 1-99)')
    return AWAIT_COPIES

async def receive_copies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 99):
        await update.message.reply_text('Please enter a number between 1 and 99.')
        return AWAIT_COPIES
    ctx.user_data['copies'] = int(text)
    await update.message.reply_text(
        'Paper size?',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('A4', callback_data='a4'),
            InlineKeyboardButton('A3', callback_data='a3'),
        ]])
    )
    return AWAIT_PAPER

async def receive_paper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data['paper_size'] = update.callback_query.data
    await update.callback_query.message.reply_text('Any special instructions? Or send /skip.')
    return AWAIT_NOTES

async def receive_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['special_notes'] = update.message.text
    return await show_summary(update, ctx)

async def skip_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['special_notes'] = None
    return await show_summary(update, ctx)

async def show_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.user_data
    summary = (
        f'Order summary:\n\n'
        f'File: {d["file_name"]}\n'
        f'Colour: {"B&W" if d["colour_mode"]=="bw" else "Colour"}\n'
        f'Sides: {d["sides"].title()}\n'
        f'Copies: {d["copies"]}\n'
        f'Paper: {d["paper_size"].upper()}\n'
        f'Notes: {d["special_notes"] or "None"}\n\n'
        'Confirm your order?'
    )
    target = update.message if update.message else update.callback_query.message
    await target.reply_text(
        summary,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('Confirm Order', callback_data='confirm'),
            InlineKeyboardButton('Cancel', callback_data='cancel_order'),
        ]])
    )
    return CONFIRM

async def confirm_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    msg = await update.callback_query.message.reply_text('Uploading your file, please wait...')
    
    d = ctx.user_data
    tg_file = await ctx.bot.get_file(d['file_id'])
    file_bytes = await tg_file.download_as_bytearray()
    file_key = await upload_file(bytes(file_bytes), d['file_name'])
    
    order_number = next_order_number()
    user = update.callback_query.from_user
    sb.table('orders').insert({
        'order_number':     order_number,
        'customer_chat_id': user.id,
        'customer_name':    user.first_name,
        'file_name':        d['file_name'],
        'file_key':         file_key,
        'file_size_bytes':  d['file_size'],
        'colour_mode':      d['colour_mode'],
        'sides':            d['sides'],
        'copies':           d['copies'],
        'paper_size':       d['paper_size'],
        'special_notes':    d.get('special_notes'),
        'status':           'pending',
    }).execute()

    await msg.edit_text(f'Order {order_number} confirmed! We will notify you when it is ready.')
    ctx.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await (update.message or update.callback_query.message).reply_text('Order cancelled.')
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('print', print_cmd)],
        states={
            AWAIT_FILE:   [MessageHandler(filters.Document.ALL | filters.PHOTO, receive_file)],
            AWAIT_COLOUR: [CallbackQueryHandler(receive_colour)],
            AWAIT_SIDES:  [CallbackQueryHandler(receive_sides)],
            AWAIT_COPIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_copies)],
            AWAIT_PAPER:  [CallbackQueryHandler(receive_paper)],
            AWAIT_NOTES:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes), CommandHandler('skip', skip_notes)],
            CONFIRM:      [CallbackQueryHandler(confirm_order, pattern='confirm'), CallbackQueryHandler(cancel, pattern='cancel_order')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv)
    
    print("Bot is starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()