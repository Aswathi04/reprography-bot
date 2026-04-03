import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from telegram import Bot

# Load environment variables from .env
load_dotenv()

# Initialize Supabase and Telegram Bot
sb  = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])
bot = Bot(os.environ['BOT_TOKEN'])
BUCKET = 'print-files'

app = FastAPI()

# ── CORS SETTINGS ─────────────────────────────────────────────
# This fixes the "blocked by CORS policy" error by allowing 
# the Antigravity dashboard to communicate with this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows Antigravity/Vercel to access the API
    allow_credentials=True,
    allow_methods=["*"],  # Allows GET, POST, OPTIONS, etc.
    allow_headers=["*"],
)
# ──────────────────────────────────────────────────────────────

@app.get('/orders/{order_id}/signed-url')
async def get_signed_url(order_id: str):
    """Generates a temporary download link for the shopkeeper"""
    res = sb.table('orders').select('file_key,file_deleted') \
            .eq('id', order_id).single().execute()
    
    if not res.data or res.data['file_deleted'] or not res.data['file_key']:
        raise HTTPException(status_code=404, detail='File not found')
    
    signed = sb.storage.from_(BUCKET).create_signed_url(
        path=res.data['file_key'],
        expires_in=7200  # URL valid for 2 hours
    )
    return {'url': signed['signedURL']}

@app.post('/orders/{order_id}/start')
async def start_order(order_id: str):
    """Marks an order as 'in_progress'"""
    sb.table('orders').update({'status': 'in_progress'}) \
      .eq('id', order_id).execute()
    return {'ok': True}

@app.post('/orders/{order_id}/complete')
async def complete_order(order_id: str):
    """Marks order done, deletes the file, and notifies the customer"""
    # Fetch order details to get the customer's chat ID
    res = sb.table('orders').select('*').eq('id', order_id).single().execute()
    order = res.data
    
    if not order:
        raise HTTPException(status_code=404, detail='Order not found')
    
    # Delete file from Supabase storage to save space
    if order['file_key'] and not order['file_deleted']:
        try:
            sb.storage.from_(BUCKET).remove([order['file_key']])
        except Exception:
            pass # Continue even if file removal fails

    # Update database record
    sb.table('orders').update({
        'status':       'completed',
        'file_deleted': True,
        'file_key':     None,
        'completed_at': 'now()',
        'notified_at':  'now()',
    }).eq('id', order_id).execute()
    
    # Send Telegram notification to the customer
    await bot.send_message(
        chat_id=order['customer_chat_id'],
        text=(
            f'Your order {order["order_number"]} is ready for collection!\n\n'
            f'File: {order["file_name"]}\n'
            'Please visit the shop to collect your prints.'
        )
    )
    return {'ok': True}

@app.get('/health')
def health():
    """Basic health check for Railway deployment"""
    return {'status': 'ok'}