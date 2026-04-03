import os, datetime
from dotenv import load_dotenv
from supabase import create_client
from telegram import Bot
import asyncio

load_dotenv()
sb  = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])
SHOPKEEPER_CHAT_ID = os.environ['SHOPKEEPER_CHAT_ID']
BUCKET = 'print-files'

async def send_alert(bot, message):
    await bot.send_message(chat_id=SHOPKEEPER_CHAT_ID, text=message)

def keepalive():
    sb.table('orders').select('id').limit(1).execute()
    print('Keepalive ping sent')

def cleanup_stale():
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=48)).isoformat()
    stale = sb.table('orders').select('*') \
              .eq('status','pending').lt('created_at', cutoff).execute().data
    count = 0
    for order in stale:
        if order['file_key'] and not order['file_deleted']:
            try:
                sb.storage.from_(BUCKET).remove([order['file_key']])
            except Exception as e:
                print(f'Error deleting file: {e}')
        sb.table('orders').update({
            'status': 'expired', 'file_deleted': True, 'file_key': None
        }).eq('id', order['id']).execute()
        count += 1
    print(f'Cleaned up {count} stale orders')
    return count

def check_usage():
    now = datetime.datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0).isoformat()
    orders_count = len(sb.table('orders').select('id') \
                         .gte('created_at', month_start).execute().data)
    files_count = len(sb.table('orders').select('id') \
                        .gte('created_at', month_start)
                        .neq('file_key', 'null').execute().data)
    sb.table('usage_snapshots').insert({
        'orders_this_month': orders_count,
        'files_this_month':  files_count,
        'alert_sent':        False,
    }).execute()
    return orders_count

async def main():
    bot = Bot(os.environ['BOT_TOKEN'])
    try:
        keepalive()
        deleted = cleanup_stale()
        orders  = check_usage()
        sb.table('cron_log').insert({
            'job_name': 'daily',
            'success':  True,
            'note':     f'{deleted} files deleted, {orders} orders this month'
        }).execute()
        await send_alert(bot, f'Daily maintenance done. {deleted} stale files cleaned up.')
    except Exception as e:
        sb.table('cron_log').insert({
            'job_name': 'daily', 'success': False, 'note': str(e)
        }).execute()
        await send_alert(bot, f'Cron job failed: {str(e)}')

if __name__ == '__main__':
    asyncio.run(main())
