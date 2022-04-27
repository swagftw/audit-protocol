from fastapi import FastAPI, Request, Response, Header
from utils.rabbitmq_utils import get_rabbitmq_connection, get_rabbitmq_channel
from config import settings
from utils import redis_keys
from utils import helper_functions
from utils import diffmap_utils
from utils import dag_utils
from utils.ipfs_async import client as ipfs_client
from utils.redis_conn import RedisPool
from aio_pika import ExchangeType, DeliveryMode, Message
from functools import partial
from aio_pika.pool import Pool
from typing import Optional
from data_models import PayloadCommit, PendingTransaction
from aioredis.lock import Lock as aioredisLock
import aioredis
import asyncio
import aiohttp
import logging
import sys
import json
import os

app = FastAPI()

formatter = logging.Formatter(u"%(levelname)-8s %(name)-4s %(asctime)s,%(msecs)d %(module)s-%(funcName)s: %(message)s")

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.DEBUG)
# stdout_handler.setFormatter(formatter)

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
# stderr_handler.setFormatter(formatter)
rest_logger = logging.getLogger(__name__)
rest_logger.setLevel(logging.DEBUG)
rest_logger.addHandler(stdout_handler)
rest_logger.addHandler(stderr_handler)

REDIS_WRITER_CONN_CONF = {
    "host": settings.redis.host,
    "port": settings.redis.port,
    "password": settings.redis.password,
    "db": settings.redis.db
}

REDIS_READER_CONN_CONF = {
    "host": settings.redis_reader.host,
    "port": settings.redis_reader.port,
    "password": settings.redis_reader.password,
    "db": settings.redis_reader.db
}


@app.on_event('startup')
async def startup_boilerplate():
    app.rmq_connection_pool = Pool(get_rabbitmq_connection, max_size=5, loop=asyncio.get_running_loop())
    app.rmq_channel_pool = Pool(
        partial(get_rabbitmq_channel, app.rmq_connection_pool), max_size=20, loop=asyncio.get_running_loop()
    )
    try:
        os.stat(os.getcwd() + '/bloom_filter_objects')
    except:
        os.mkdir(os.getcwd() + '/bloom_filter_objects')

    try:
        os.stat(os.getcwd() + '/containers')
    except:
        os.mkdir(os.getcwd() + '/containers')
    app.aioredis_pool = RedisPool()
    await app.aioredis_pool.populate()
    app.writer_redis_pool = app.aioredis_pool.writer_redis_pool
    app.reader_redis_pool = app.aioredis_pool.reader_redis_pool
    app.aiohttp_client_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(
            sock_connect=settings.aiohtttp_timeouts.sock_connect,
            sock_read=settings.aiohtttp_timeouts.sock_read,
            connect=settings.aiohtttp_timeouts.connect
        )
    )


@app.post('/')
async def create_dag(
        request: Request,
        response: Response,
        x_hook_signature: str = Header(None),
):
    reader_redis_conn: aioredis.Redis = request.app.reader_redis_pool
    writer_redis_conn: aioredis.Redis = request.app.writer_redis_pool
    event_data = await request.json()
    response_body = dict()
    response_status_code = 200
    # Verify the payload that has arrived.
    if x_hook_signature:
        is_safe = dag_utils.check_signature(event_data, x_hook_signature)
        if is_safe:
            rest_logger.debug("The arriving payload has been verified")
            pass
        else:
            rest_logger.debug("Recieved an wrong signature payload")
            response.status_code = response_status_code
            response_body = {'status': 'BadXHookSignature'}
            return response_body
    if 'event_name' in event_data.keys():
        if event_data['event_name'] == 'RecordAppended':
            rest_logger.debug(event_data)

            """ Get data from the event """
            project_id = event_data['event_data']['projectId']
            tentative_block_height_event_data = int(event_data['event_data']['tentativeBlockHeight'])

            # Get the max block height(finalized after all error corrections and reorgs) for the project_id
            finalized_block_height_project = await helper_functions.get_block_height(
                project_id=project_id,
                reader_redis_conn=reader_redis_conn
            )

            rest_logger.debug(
                "Event Data Tentative Block Height: %s | Finalized Project %s Block Height: %s",
                tentative_block_height_event_data, project_id, finalized_block_height_project
            )

            # tentative_block_height_cached = await helper_functions.get_tentative_block_height(
            #     project_id=project_id,
            #     reader_redis_conn=reader_redis_conn
            # )
            # acquire project ID processing lock
            lock = aioredisLock(
                redis=request.app.writer_redis_pool,
                name=project_id,
                blocking_timeout=5,  # should not need more than 5 seconds of waiting on acquiring a lock
                timeout=10  # entire operation should not take more than 10 seconds
            )
            ret = await lock.acquire()
            if not ret:
                response_status_code = 500
                response_body = dict()
                response.status_code = response_status_code
                return response_body
            # retrieve callback URL for project ID
            cb_url = await reader_redis_conn.get(f'powerloom:project:{project_id}:callbackURL')
            if tentative_block_height_event_data <= finalized_block_height_project:
                rest_logger.debug("Discarding event at height %s | %s", tentative_block_height_event_data, event_data)                
                await dag_utils.discard_event(
                    project_id=project_id,
                    payload_commit_id=event_data['event_data']['payloadCommitId'],
                    tx_hash=event_data['txHash'],
                    payload_cid=event_data['event_data']['snapshotCid'],
                    tentative_block_height=tentative_block_height_event_data,
                    writer_redis_conn=writer_redis_conn
                )
                # rest_logger.debug("Redis operations output: ")
                # rest_logger.debug(redis_output)
                response_body = {'status': 'Discarded', 'reason': 'Tentative height lower than finalized height'}
            elif tentative_block_height_event_data > finalized_block_height_project + 1:
                rest_logger.debug(
                    "An out of order event arrived at tentative height %s | Project ID %s | "
                    "Current finalized height: %s",
                    tentative_block_height_event_data, project_id, finalized_block_height_project
                )

                discarded_transactions_key = redis_keys.get_discarded_transactions_key(project_id)

                rest_logger.debug("Checking if txHash %s is in the list of pending Transactions", event_data['txHash'])

                _ = await reader_redis_conn.zrangebyscore(
                    name=redis_keys.get_pending_transactions_key(project_id),
                    min=float('-inf'),
                    max=float('+inf'),
                    withscores=False
                )
                is_pending = False
                # will be used to reset lastTouchBlocked tag in pending set
                # we will use the raw bytes entry to safely address the member in the zset
                pending_tx_set_entry: Optional[bytes] = None
                for k in _:
                    pending_tx_obj: PendingTransaction = PendingTransaction.parse_raw(k)
                    if pending_tx_obj.txHash == event_data['txHash']:
                        is_pending = True
                        pending_tx_set_entry = k
                        break

                if not is_pending:
                    rest_logger.error(
                        "Discarding event because tx Hash not in pending transactions | Project ID %s | %s",
                        project_id, event_data
                    )
                    _ = await dag_utils.clear_payload_commit_data(
                        project_id=project_id,
                        tx_hash=event_data['txHash'],
                        tentative_height_pending_tx_entry=0,  # this has no effect, and it is fine
                        payload_commit_id=event_data['event_data']['payloadCommitId'],
                        writer_redis_conn=writer_redis_conn
                    )

                    _ = await writer_redis_conn.zadd(
                        name=discarded_transactions_key,
                        mapping={event_data['txHash']: tentative_block_height_event_data}
                    )

                    response_body = {'status': 'Discarded', 'reason': 'Not found in pending transaction set'}
                else:
                    rest_logger.debug(
                        "Saving Event data for tentative height: %s | %s ",
                        tentative_block_height_event_data, event_data
                    )
                    await dag_utils.save_event_data(
                        event_data=event_data,
                        writer_redis_conn=writer_redis_conn,
                        pending_tx_set_entry=pending_tx_set_entry
                    )
                    response_body = {
                        'status': 'Enqueued',
                        'event_tentative_height': tentative_block_height_event_data,
                        'finalized_height': finalized_block_height_project
                    }
                    # first, we get transactions in the 'pending' state
                    pending_confirmation_callbacks_txs = await reader_redis_conn.zrangebyscore(
                        name=redis_keys.get_pending_transactions_key(project_id),
                        min=finalized_block_height_project + 1,
                        max=tentative_block_height_event_data,
                        withscores=True
                    )
                    # filter out prior resubmitted transactions not older than 3 blocks
                    # or if they are 'fresh' as well (lastTouchedBlock = 0) yet 3 blocks have passed since confirmation
                    # has arrived (the score of the pending tx entry is used for this, which is the tentative block
                    # height at which the tx callback is supposed to arrive)
                    # not to be considered in this set are the ones already enqueued for block creation
                    # (lastTouchedBlock == -1)
                    pending_confirmation_callbacks_txs_filtered = list(filter(
                        lambda x:
                        (PendingTransaction.parse_raw(x[0]).lastTouchedBlock != -1 and
                         PendingTransaction.parse_raw(x[0]).lastTouchedBlock != 0 and
                         PendingTransaction.parse_raw(x[0]).lastTouchedBlock + 3 <= tentative_block_height_event_data
                         )
                        or (PendingTransaction.parse_raw(x[0]).lastTouchedBlock == 0 and
                            int(x[1]) + 3 <= tentative_block_height_event_data),
                        pending_confirmation_callbacks_txs
                    ))
                    rest_logger.info(
                        'Pending transactions qualified for resubmission on account of being unconfirmed or '
                        'not yet considered since the last resubmission attempt: %s',
                        pending_confirmation_callbacks_txs_filtered
                    )
                    # map these tx entries eligible for resubmission to their tentative block height scores
                    if pending_confirmation_callbacks_txs_filtered:
                        pending_confirmation_callbacks_txs_filtered_map = {
                            int(x[1]): x[0] for x in pending_confirmation_callbacks_txs_filtered
                        }
                        rest_logger.info(
                            'Preparing to send out txs for reprocessing for the ones that have not '
                            'received any callbacks (possibility of dropped txs) : %s',
                            pending_confirmation_callbacks_txs_filtered
                        )
                        # send out txs for reprocessing
                        republished_txs = list()
                        replaced_dummy_tx_entries_in_pending_set = list()
                        async with request.app.rmq_channel_pool.acquire() as channel:
                            exchange = await channel.declare_exchange(
                                settings.rabbitmq.setup['core']['exchange'],
                                ExchangeType.DIRECT
                            )
                            for queued_tentative_height_ in pending_confirmation_callbacks_txs_filtered_map.keys():
                                # get the tx hash from the filtered set of qualified pending transactions
                                single_pending_tx_entry = pending_confirmation_callbacks_txs_filtered_map[
                                    queued_tentative_height_]
                                pending_tx_obj: PendingTransaction = PendingTransaction.parse_raw(
                                    single_pending_tx_entry
                                )
                                tx_hash = pending_tx_obj.txHash
                                # fetch transaction input data
                                tx_commit_details = pending_tx_obj.event_data
                                if not tx_commit_details:
                                    rest_logger.critical(
                                        'Possible irrecoverable gap in DAG chain creation %s | '
                                        'Did not find cached input data against past tx: %s | '
                                        'Last finalized height: %s | '
                                        'Qualified txs for resubmission',
                                        project_id, tx_hash,
                                        finalized_block_height_project,
                                        pending_confirmation_callbacks_txs_filtered_map
                                    )
                                    continue
                                # send once more to payload commit service
                                payload_commit_obj = PayloadCommit(
                                    **{
                                        'projectId': project_id,
                                        'commitId': tx_commit_details.payloadCommitId,
                                        'snapshotCID': tx_commit_details.snapshotCid,
                                        'tentativeBlockHeight': tx_commit_details.tentativeBlockHeight,
                                        'apiKeyHash': tx_commit_details.apiKeyHash,
                                        'resubmitted': True,
                                        'resubmissionBlock': tentative_block_height_event_data
                                    }
                                )
                                message = Message(
                                    payload_commit_obj.json().encode('utf-8'),
                                    delivery_mode=DeliveryMode.PERSISTENT,
                                )
                                await exchange.publish(
                                    message=message,
                                    routing_key='commit-payloads'
                                )
                                rest_logger.debug(
                                    'Re-Published payload against commit ID %s , tentative block height %s for '
                                    'reprocessing by payload commit service | '
                                    'Previous tx not found for DAG entry: %s',
                                    tx_commit_details.payloadCommitId, tx_commit_details.tentativeBlockHeight,
                                    tx_hash
                                )
                                republished_txs.append({
                                    'txHash': tx_hash,
                                    'payloadCommitID': payload_commit_obj.commitId,
                                    'unconfirmedTentativeHeight': tx_commit_details.tentativeBlockHeight,
                                    'resubmittedAtConfirmedBlockHeight': tentative_block_height_event_data
                                })
                                # NOTE: dont remove hash from pending transactions key, instead overwrite with
                                #       resubmission attempt block number so the next time an event with
                                #       higher tentative block height comes through it does not include this
                                #       entry for a resubmission attempt (once payload commit service takes
                                #       care of the resubmission, the pending entry is also updated with the
                                #       new tx hash)
                                rest_logger.info(
                                    'Replacing with dummy tx hash entry %s at height %s '
                                    'to avoid intermediate resubmission attempts',
                                    tx_hash, queued_tentative_height_
                                )
                                update_res = await dag_utils.update_pending_tx_block_touch(
                                    pending_tx_set_entry=single_pending_tx_entry,
                                    touched_at_block=tentative_block_height_event_data,
                                    tentative_block_height=queued_tentative_height_,
                                    project_id=project_id,
                                    writer_redis_conn=writer_redis_conn,
                                    event_data=pending_tx_obj.event_data
                                )
                                if update_res['status']:
                                    rest_logger.info(
                                        'Removed tx hash entry %s from pending transactions set',
                                        single_pending_tx_entry
                                    )
                                    rest_logger.info(
                                        'Successfully replaced with dummy tx hash entry at height %s '
                                        'to avoid intermediate resubmission attempts in pending txs set',
                                        queued_tentative_height_
                                    )
                                    replaced_dummy_tx_entries_in_pending_set.append({
                                        'atHeight': queued_tentative_height_
                                    })
                                else:
                                    if not update_res['results']['zrem']:
                                        rest_logger.warning(
                                            'Could not remove tx hash entry %s from pending transactions set',
                                            single_pending_tx_entry
                                        )
                                    if not update_res['results']['zadd']:
                                        rest_logger.warning(
                                            'Failed to replace with dummy tx hash entry at height %s '
                                            'to avoid intermediate resubmission attempts in pending txs set',
                                            queued_tentative_height_
                                        )

                        response_body.update({
                            'republishedTxs': republished_txs,
                            'replacedDummyEntries': replaced_dummy_tx_entries_in_pending_set
                        })
            elif tentative_block_height_event_data == finalized_block_height_project + 1:
                """
                    An event which is in-order has arrived. Create a dag block for this event
                    and process all other pending events for this project
                """
                pending_tx_set_entry = None  # will be used to reset lastTouchBlocked tag in pending set
                all_pending_tx_entries = await reader_redis_conn.zrangebyscore(
                    name=redis_keys.get_pending_transactions_key(project_id),
                    min=float('-inf'),
                    max=float('+inf'),
                    withscores=False
                )
                # rest_logger.debug('All pending transactions for project %s in key %s : %s',
                #                   project_id, redis_keys.get_pending_transactions_key(project_id),
                #                   all_pending_tx_entries)
                is_pending = False
                for k in all_pending_tx_entries:
                    pending_tx_obj: PendingTransaction = PendingTransaction.parse_raw(k)
                    # rest_logger.debug('Comparing event data tx hash %s with pending tx obj tx hash %s '
                    #                   '| tx obj itself: %s',
                    #                   event_data['txHash'], pending_tx_obj.txHash, pending_tx_obj)
                    if pending_tx_obj.txHash == event_data['txHash']:
                        is_pending = True
                        pending_tx_set_entry = k
                        break
                if not is_pending:
                    discarded_transactions_key = redis_keys.get_discarded_transactions_key(project_id)

                    rest_logger.error(
                        "Discarding event because tx Hash not in pending transactions | Project ID %s | %s",
                        project_id, event_data
                    )
                    _ = await dag_utils.clear_payload_commit_data(
                        project_id=project_id,
                        tx_hash=event_data['txHash'],
                        tentative_height_pending_tx_entry=0,  # this has no effect, and it is fine
                        payload_commit_id=event_data['event_data']['payloadCommitId'],
                        writer_redis_conn=writer_redis_conn
                    )

                    _ = await writer_redis_conn.zadd(
                        name=discarded_transactions_key,
                        mapping={event_data['txHash']: tentative_block_height_event_data}
                    )

                    response_body = {'status': 'Discarded', 'reason': 'Not found in pending transaction set'}
                else:
                    _dag_cid, dag_block = await dag_utils.create_dag_block(
                        tx_hash=event_data['txHash'],
                        project_id=project_id,
                        tentative_block_height=tentative_block_height_event_data,
                        payload_cid=event_data['event_data']['snapshotCid'],
                        timestamp=event_data['event_data']['timestamp'],
                        reader_redis_conn=reader_redis_conn,
                        writer_redis_conn=writer_redis_conn
                    )
                    rest_logger.info('Created DAG block with CID %s at height %s', _dag_cid,
                                     tentative_block_height_event_data)

                    # clear payload commit ID log on success
                    await dag_utils.clear_payload_commit_processing_logs(
                        project_id=project_id,
                        payload_commit_id=event_data['event_data']['payloadCommitId'],
                        writer_redis_conn=writer_redis_conn
                    )

                    # clear from pending set
                    _ = await writer_redis_conn.zrem(
                        redis_keys.get_pending_transactions_key(project_id), pending_tx_set_entry
                    )
                    if _:
                        rest_logger.debug(
                            'Cleared tx entry for tx %s from pending set at block height %s | project ID %s',
                            event_data['txHash'], tentative_block_height_event_data, project_id
                        )
                    else:
                        rest_logger.debug(
                            'Not sure if tx entry cleared for tx %s from pending set at block height %s | '
                            'Redis return: %s | Project ID : %s',
                            event_data['txHash'], tentative_block_height_event_data, _, project_id
                        )

                    response_body = {
                        'status': 'Inserted', 'atHeight': tentative_block_height_event_data, 'dagCID': _dag_cid
                    }

                    # process diff at this new block height
                    try:
                        diff_map = await diffmap_utils.calculate_diff(
                            dag_cid=_dag_cid,
                            dag=dag_block,
                            project_id=project_id,
                            ipfs_client=ipfs_client,
                            writer_redis_conn=writer_redis_conn
                        )
                    except json.decoder.JSONDecodeError as jerr:
                        rest_logger.debug("There was an error while decoding the JSON data: %s", jerr, exc_info=True)
                    else:
                        rest_logger.debug("The diff map retrieved: %s", diff_map)

                    # send commit ID confirmation callback
                    if cb_url:
                        await send_commit_callback(
                            aiohttp_session=request.app.aiohttp_client_session,
                            url=cb_url,
                            payload={
                                'commitID': event_data['event_data']['payloadCommitId'],
                                'projectID': project_id,
                                'status': True
                            }
                        )

                    """ retrieve all list of all the payload_commit_ids from the pendingBlocks zset """
                    # get txs from higher heights which have received a confirmation callback
                    # and form a continous sequence
                    # hence, are ready to be added to chain
                    all_pending_txs = await reader_redis_conn.zrangebyscore(
                        name=redis_keys.get_pending_transactions_key(project_id),
                        min=tentative_block_height_event_data+1,
                        max='+inf',
                        withscores=True
                    )
                    all_qualified_dag_addition_txs = filter(
                        lambda x: PendingTransaction.parse_raw(x[0]).lastTouchedBlock == -1,
                        all_pending_txs
                    )
                    pending_blocks_finalized = list()
                    cur_max_height_project = tentative_block_height_event_data
                    for pending_tx_entry, _tt_block_height in all_qualified_dag_addition_txs:
                        pending_tx_obj: PendingTransaction = PendingTransaction.parse_raw(pending_tx_entry)
                        _tt_block_height = int(_tt_block_height)

                        if _tt_block_height == cur_max_height_project + 1:
                            rest_logger.info(
                                'Processing queued confirmed tx %s at tentative_block_height: %s',
                                pending_tx_obj, _tt_block_height
                            )

                            """ Create the dag block for this event """
                            _dag_cid, dag_block = await dag_utils.create_dag_block(
                                tx_hash=pending_tx_obj.event_data.txHash,
                                project_id=project_id,
                                tentative_block_height=_tt_block_height,
                                payload_cid=pending_tx_obj.event_data.snapshotCid,
                                timestamp=int(pending_tx_obj.event_data.timestamp),
                                reader_redis_conn=reader_redis_conn,
                                writer_redis_conn=writer_redis_conn
                            )
                            rest_logger.info('Created enqueued DAG block with CID %s at height %s', _dag_cid,
                                             _tt_block_height)
                            pending_blocks_finalized.append({
                                'status': 'Inserted', 'atHeight': _tt_block_height, 'dagCID': _dag_cid
                            })
                            cur_max_height_project = _tt_block_height

                            try:
                                diff_map = await diffmap_utils.calculate_diff(
                                    dag_cid=_dag_cid,
                                    dag=dag_block,
                                    project_id=project_id,
                                    ipfs_client=ipfs_client,
                                    writer_redis_conn=writer_redis_conn
                                )
                            except json.decoder.JSONDecodeError as jerr:
                                rest_logger.debug("There was an error while decoding the JSON data: %s", jerr,
                                                  exc_info=True)
                            else:
                                rest_logger.debug("The diff map retrieved: %s", diff_map)

                            await dag_utils.clear_payload_commit_processing_logs(
                                project_id=project_id,
                                payload_commit_id=pending_tx_obj.event_data.payloadCommitId,
                                writer_redis_conn=writer_redis_conn
                            )
                            # clear from pending set
                            _ = await writer_redis_conn.zrem(
                                redis_keys.get_pending_transactions_key(project_id),
                                pending_tx_entry
                            )
                            if _:
                                rest_logger.debug(
                                    'Cleared tx entry for tx %s from pending set at block height %s | project ID %s',
                                    pending_tx_obj.txHash, _tt_block_height, project_id
                                )
                            else:
                                rest_logger.debug(
                                    'Not sure if tx entry cleared for tx %s from pending set at block height %s | '
                                    'Redis return: %s | Project ID : %s',
                                    pending_tx_obj.txHash, _tt_block_height, _, project_id
                                )
                            # send commit ID confirmation callback
                            if cb_url:
                                await send_commit_callback(
                                    aiohttp_session=request.app.aiohttp_client_session,
                                    url=cb_url,
                                    payload={
                                        'commitID': pending_tx_obj.event_data.payloadCommitId,
                                        'projectID': project_id,
                                        'status': True
                                    }
                                )
                    response_body.update({'pendingBlocksFinalized': pending_blocks_finalized})
            # release aioredis lock
            await lock.release()
    response.status_code = response_status_code
    return response_body


async def send_commit_callback(aiohttp_session: aiohttp.ClientSession, url, payload):
    if type(url) is bytes:
        url = url.decode('utf-8')
    try:
        async with aiohttp_session.post(url=url, json=payload) as resp:
            json_response = await resp.json()
    except Exception as e:
        rest_logger.error('Failed to push callback for commit ID')
        rest_logger.error({'url': url, 'payload': payload})
        rest_logger.error(e, exc_info=True)
