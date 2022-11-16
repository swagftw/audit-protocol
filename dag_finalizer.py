from data_models import DiffCalculationRequest
from fastapi import FastAPI, Request, Response, Header
from utils.rabbitmq_utils import get_rabbitmq_connection, get_rabbitmq_channel
from config import settings
from utils import redis_keys
from utils import helper_functions
from async_ipfshttpclient.main import AsyncIPFSClientSingleton
from utils import dag_utils
from utils.redis_conn import RedisPool
from aio_pika import ExchangeType, DeliveryMode, Message
from tenacity import retry_if_exception, wait_random_exponential, stop_after_attempt, retry, AsyncRetrying, wait_random
from functools import partial
from aio_pika.pool import Pool
from typing import Optional
from data_models import PayloadCommit, PendingTransaction, ProjectDAGChainSegmentMetadata
from multiprocessing import Manager
from redis import asyncio as aioredis
import asyncio
import aiohttp
import logging
import sys
import json
import os
from utils.dag_utils import DAGCreationException


class RedisLockAcquisitionFailure(Exception):
    pass


app = FastAPI()

formatter = logging.Formatter(u"%(levelname)-8s %(name)-4s %(asctime)s,%(msecs)d %(module)s-%(funcName)s: %(message)s")

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.DEBUG)
# not setting formatter here since logs are intercepted by loguru in the gunicorn launcher script and further adapted
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
multiprocess_manager = Manager()
project_specific_locks_multiprocessing_map = multiprocess_manager.dict()


class CustomAdapter(logging.LoggerAdapter):
    """
    This example adapter expects the passed in dict-like object to have a
    'txHash' key, whose value in brackets is prepended to the log message.
    """

    def process(self, msg, kwargs):
        return '[%s] %s' % (self.extra['txHash'], msg), kwargs


@app.on_event('startup')
async def startup_boilerplate():
    app.rmq_connection_pool = Pool(get_rabbitmq_connection, max_size=5, loop=asyncio.get_running_loop())
    app.rmq_channel_pool = Pool(
        partial(get_rabbitmq_channel, app.rmq_connection_pool), max_size=20, loop=asyncio.get_running_loop()
    )

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
    app.ipfs_singleton = AsyncIPFSClientSingleton()
    await app.ipfs_singleton.init_sessions()
    app.ipfs_writer_client = app.ipfs_singleton._ipfs_write_client
    app.ipfs_reader_client = app.ipfs_singleton._ipfs_read_client


@retry(
    reraise=True,
    wait=wait_random_exponential(multiplier=1, min=15, max=30),
    retry=retry_if_exception(RedisLockAcquisitionFailure)
)
async def payload_to_dag_processor_task(event_data):
    """ Get data from the event """
    project_id = event_data['event_data']['projectId']
    tx_hash = event_data['txHash']
    request_id = event_data['requestID']
    asyncio.current_task(asyncio.get_running_loop()).set_name('TxProcessor-'+tx_hash)
    custom_logger = CustomAdapter(rest_logger, {'txHash': tx_hash})
    tentative_block_height_event_data = int(event_data['event_data']['tentativeBlockHeight'])
    writer_redis_conn: aioredis.Redis = app.writer_redis_pool
    reader_redis_conn: aioredis.Redis = app.reader_redis_pool
    # acquire project ID processing lock
    lock = aioredis.lock.Lock(
        redis=writer_redis_conn,
        name=project_id,
        blocking_timeout=5,  # should not need more than 5 seconds of waiting on acquiring a lock
        timeout=settings.webhook_listener.redis_lock_lifetime
    )
    ret = await lock.acquire()
    if not ret:
        raise RedisLockAcquisitionFailure

    # Get the max block height(finalized after all error corrections and reorgs) for the project_id
    finalized_block_height_project = await helper_functions.get_block_height(
        project_id=project_id,
        reader_redis_conn=reader_redis_conn
    )

    custom_logger.debug(
        "Event Data Tentative Block Height: %s | Finalized Project %s Block Height: %s",
        tentative_block_height_event_data, project_id, finalized_block_height_project
    )

    # tentative_block_height_cached = await helper_functions.get_tentative_block_height(
    #     project_id=project_id,
    #     reader_redis_conn=reader_redis_conn
    # )

    # retrieve callback URL for project ID
    cb_url = await reader_redis_conn.get(f'powerloom:project:{project_id}:callbackURL')
    if tentative_block_height_event_data <= finalized_block_height_project:
        custom_logger.debug("Discarding event at height %s | %s", tentative_block_height_event_data, event_data)
        await dag_utils.discard_event(
            project_id=project_id,
            request_id=request_id,
            tentative_block_height=tentative_block_height_event_data,
            writer_redis_conn=writer_redis_conn
        )
        # custom_logger.debug("Redis operations output: ")
        # custom_logger.debug(redis_output)
        #response_body = {'status': 'Discarded', 'reason': 'Tentative height lower than finalized height'}
    elif tentative_block_height_event_data > finalized_block_height_project + 1:
        custom_logger.debug(
            "An out of order event arrived at tentative height %s | Project ID %s | "
            "Current finalized height: %s",
            tentative_block_height_event_data, project_id, finalized_block_height_project
        )

        discarded_transactions_key = redis_keys.get_discarded_transactions_key(project_id)

        custom_logger.debug("Checking if requestID %s is in the list of pending Transactions", request_id)

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
            if pending_tx_obj.requestID != "":
                if pending_tx_obj.requestID == request_id:
                    is_pending = True
                    pending_tx_set_entry = k
                    break
            else:
                if pending_tx_obj.event_data.tentativeBlockHeight == tentative_block_height_event_data:
                    is_pending = True
                    pending_tx_set_entry = k
                    break

        if not is_pending:
            custom_logger.error(
                "Discarding event because requestID %s not in pending transactions | Project ID %s | %s",
                request_id, project_id, event_data
            )
            _ = await dag_utils.clear_payload_commit_data(
                project_id=project_id,
                tentative_height_pending_tx_entry=0,  # this has no effect, and it is fine
                writer_redis_conn=writer_redis_conn
            )

            _ = await writer_redis_conn.zadd(
                name=discarded_transactions_key,
                mapping={request_id: tentative_block_height_event_data}
            )

        else:
            custom_logger.debug(
                "Saving Event data for tentative height: %s | %s ",
                tentative_block_height_event_data, event_data
            )
            await dag_utils.save_event_data(
                event_data=event_data,
                writer_redis_conn=writer_redis_conn,
                pending_tx_set_entry=pending_tx_set_entry
            )

            # first, we get transactions in the 'pending' state
            pending_confirmation_callbacks_txs = await reader_redis_conn.zrangebyscore(
                name=redis_keys.get_pending_transactions_key(project_id),
                min=finalized_block_height_project + 1,
                max=tentative_block_height_event_data,
                withscores=True
            )
            # filter out prior resubmitted transactions not older than 10 blocks
            # or if they are 'fresh' as well (lastTouchedBlock = 0) yet 10 blocks have passed since confirmation
            # has arrived (the score of the pending tx entry is used for this, which is the tentative block
            # height at which the tx callback is supposed to arrive)
            # not to be considered in this set are the ones already enqueued for block creation
            # (lastTouchedBlock == -1)
            num_block_to_wait_for_resubmission = 10
            pending_confirmation_callbacks_txs_filtered = list(filter(
                lambda x:
                    PendingTransaction.parse_raw(x[0]).lastTouchedBlock == 0 and
                    int(x[1]) + num_block_to_wait_for_resubmission <= tentative_block_height_event_data,
                pending_confirmation_callbacks_txs
            ))
            custom_logger.info(
                'Pending transactions qualified for resubmission on account of being unconfirmed or '
                'not yet considered since the last resubmission attempt: %s',
                pending_confirmation_callbacks_txs_filtered
            )
            # map these tx entries eligible for resubmission to their tentative block height scores
            if pending_confirmation_callbacks_txs_filtered:
                pending_confirmation_callbacks_txs_filtered_map = {
                    int(x[1]): x[0] for x in pending_confirmation_callbacks_txs_filtered
                }
                custom_logger.info(
                    'Preparing to send out txs for reprocessing for the ones that have not '
                    'received any callbacks (possibility of dropped txs) : %s',
                    pending_confirmation_callbacks_txs_filtered
                )
                # send out txs for reprocessing
                republished_txs = list()
                replaced_dummy_tx_entries_in_pending_set = list()
                async with app.rmq_channel_pool.acquire() as channel:
                    # to save a call to rabbitmq. we already initialize exchanges and queues beforehand
                    # always ensure exchanges and queues are initialized as part of launch sequence,
                    # not to be checked here
                    exchange = await channel.get_exchange(
                        name=settings.rabbitmq.setup['core']['exchange'],
                        ensure=False
                    )
                    for queued_tentative_height_ in pending_confirmation_callbacks_txs_filtered_map.keys():
                        # get the tx hash from the filtered set of qualified pending transactions
                        single_pending_tx_entry = pending_confirmation_callbacks_txs_filtered_map[
                            queued_tentative_height_]
                        pending_tx_obj: PendingTransaction = PendingTransaction.parse_raw(
                            single_pending_tx_entry
                        )
                        request_id = pending_tx_obj.requestID
                        # fetch transaction input data
                        tx_commit_details = pending_tx_obj.event_data
                        if not tx_commit_details:
                            custom_logger.critical(
                                'Possible irrecoverable gap in DAG chain creation %s | '
                                'Did not find cached input data against past requestID: %s | '
                                'Last finalized height: %s | '
                                'Qualified txs for resubmission',
                                project_id, request_id,
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
                                'requestID': request_id,
                                'resubmissionBlock': tentative_block_height_event_data,
                                'skipAnchorProof': tx_commit_details.skipAnchorProof
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
                        custom_logger.debug(
                            'Re-Published payload against commit ID %s , tentative block height %s for '
                            'reprocessing by payload commit service | '
                            'Tx confirmation for requestID not received : %s',
                            tx_commit_details.payloadCommitId, tx_commit_details.tentativeBlockHeight,
                            request_id
                        )
                        republished_txs.append({
                            'requestID': request_id,
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
                        custom_logger.info(
                            'Updating the requestID entry %s at height %s '
                            'to avoid intermediate resubmission attempts',
                            request_id, queued_tentative_height_
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
                            custom_logger.info(
                                'Removed requestID entry %s from pending transactions set',
                                single_pending_tx_entry
                            )
                            custom_logger.info(
                                'Successfully replaced the requestID entry at height %s '
                                'to avoid intermediate resubmission attempts in pending txs set',
                                queued_tentative_height_
                            )
                            replaced_dummy_tx_entries_in_pending_set.append({
                                'atHeight': queued_tentative_height_
                            })
                        else:
                            if not update_res['results']['zrem']:
                                custom_logger.warning(
                                    'Could not remove requestID entry %s from pending transactions set',
                                    single_pending_tx_entry
                                )
                            if not update_res['results']['zadd']:
                                custom_logger.warning(
                                    'Failed to replace the requestID entry at height %s '
                                    'to avoid intermediate resubmission attempts in pending txs set',
                                    queued_tentative_height_
                                )

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
        # custom_logger.debug('All pending transactions for project %s in key %s : %s',
        #                   project_id, redis_keys.get_pending_transactions_key(project_id),
        #                   all_pending_tx_entries)
        is_pending = False
        for k in all_pending_tx_entries:
            pending_tx_obj: PendingTransaction = PendingTransaction.parse_raw(k)
            # custom_logger.debug('Comparing event data tx hash %s with pending tx obj tx hash %s '
            #                   '| tx obj itself: %s',
            #                   event_data['txHash'], pending_tx_obj.txHash, pending_tx_obj)
            if pending_tx_obj.requestID != "":
                if pending_tx_obj.requestID == request_id:
                    is_pending = True
                    pending_tx_set_entry = k
                    break
            else:
                if pending_tx_obj.event_data.tentativeBlockHeight == tentative_block_height_event_data:
                    is_pending = True
                    pending_tx_set_entry = k
                    break
        if not is_pending:
            discarded_transactions_key = redis_keys.get_discarded_transactions_key(project_id)

            custom_logger.error(
                "Discarding event because requestID %s not in pending transactions | Project ID %s | %s",
                request_id, project_id, event_data
            )
            _ = await dag_utils.clear_payload_commit_data(
                project_id=project_id,
                tentative_height_pending_tx_entry=0,  # this has no effect, and it is fine
                writer_redis_conn=writer_redis_conn
            )

            _ = await writer_redis_conn.zadd(
                name=discarded_transactions_key,
                mapping={request_id: tentative_block_height_event_data}
            )
        else:
            fetch_prev_cid_for_dag_block_creation = True
            # check if the creation of a DAG block at this height will result in segment size overflow
            # NOTE: there might be queued pending blocks that might get processed after the first
            #       in-order insertion, where the segment size overflow condition might be satisfied
            #       Not handling it will not break anything except for the fact that we might have unevenly sized
            #       pruned and backed up segments.
            if tentative_block_height_event_data > settings.pruning.segment_size and tentative_block_height_event_data % settings.pruning.segment_size == 1:
                await identify_prune_target(project_id, tentative_block_height_event_data)
                fetch_prev_cid_for_dag_block_creation = False
            async for attempt in AsyncRetrying(
                    # we want to retry as soon since there are enough timed waits in the asyncio awaited operations
                    reraise=True, wait=wait_random(min=1, max=2),
                    retry=retry_if_exception(DAGCreationException)
            ):
                with attempt:
                    t = attempt.retry_state.attempt_number
                    if t > 1:
                        custom_logger.info(
                            'In-order DAG block creation | Project ID: %s | Tentative Height: %s | Retry attempt: %s',
                            project_id, tentative_block_height_event_data, t
                        )
                        if not await lock.owned():
                            # reacquire lock
                            if not await lock.locked() or not await lock.owned():
                                custom_logger.warning(
                                    'Project specific lock expired while retrying DAG Creation '
                                    '| Project %s | Tentative Height %s | Retry attempt number: %s',
                                    project_id, tentative_block_height_event_data, t
                                )
                                r_a = await lock.acquire()
                                if not r_a:
                                    custom_logger.warning(
                                        'Project specific lock expired while retrying DAG Creation | Failed to reacquire'
                                        '| Project %s | Tentative Height %s | Retry attempt number: %s',
                                        project_id, tentative_block_height_event_data, t
                                    )
                                    raise RedisLockAcquisitionFailure
                            # locked by (*ANY*) process and that happens to be me: # if lock.locked() and lock.owned() #
                            else:
                                # extend lock for safety
                                await lock.extend(additional_time=settings.webhook_listener.redis_lock_lifetime)
                                custom_logger.info(
                                    'In-order DAG block creation | Retry attempt: %s | Project specific lock lifetime '
                                    'extended successfully %s | Tentative height: %s',
                                    t, project_id, tentative_block_height_event_data
                                )
                    _dag_cid, dag_block = await dag_utils.create_dag_block_timebound(
                        tx_hash=tx_hash,
                        project_id=project_id,
                        tentative_block_height=tentative_block_height_event_data,
                        payload_cid=event_data['event_data']['snapshotCid'],
                        timestamp=event_data['event_data']['timestamp'],
                        reader_redis_conn=reader_redis_conn,
                        writer_redis_conn=writer_redis_conn,
                        prev_cid_fetch=fetch_prev_cid_for_dag_block_creation,
                        ipfs_write_client=app.ipfs_write_client
                    )
                    custom_logger.info('Created DAG block with CID %s at height %s', _dag_cid,
                                       tentative_block_height_event_data)
            # clear from pending set
            _ = await writer_redis_conn.zremrangebyscore(
                name=redis_keys.get_pending_transactions_key(project_id),
                min=tentative_block_height_event_data,
                max=tentative_block_height_event_data
            )
            if _:
                custom_logger.debug(
                    'Cleared entry for requestID %s from pending set at block height %s | project ID %s',
                    request_id, tentative_block_height_event_data, project_id
                )
            else:
                custom_logger.debug(
                    'Not sure if entry cleared for requestID %s from pending set at block height %s | '
                    'Redis return: %s | Project ID : %s',
                    request_id, tentative_block_height_event_data, _, project_id
                )

            # process diff at this new block height
            # send out to processing queue of diff calculation service
            if dag_block.prevCid:
                diff_calculation_request = DiffCalculationRequest(
                    project_id=project_id,
                    dagCid=_dag_cid,
                    lastDagCid=dag_block.prevCid['/'],
                    payloadCid=event_data['event_data']['snapshotCid'],
                    txHash=tx_hash,
                    timestamp=event_data['event_data']['timestamp'],
                    tentative_block_height=tentative_block_height_event_data
                )
                async with app.rmq_channel_pool.acquire() as channel:
                    # to save a call to rabbitmq. we already initialize exchanges and queues beforehand
                    exchange = await channel.get_exchange(
                        settings.rabbitmq.setup['core']['exchange']
                    )
                    message = Message(
                        diff_calculation_request.json().encode('utf-8'),
                        delivery_mode=DeliveryMode.PERSISTENT,
                    )
                    await exchange.publish(
                        message=message,
                        routing_key='diff-calculation'
                    )
                    custom_logger.debug(
                        'Published diff calculation request | At height %s | Project %s',
                        tentative_block_height_event_data, project_id
                    )
            else:
                custom_logger.debug(
                    'No diff calculation request to publish for first block | At height %s | Project %s',
                    tentative_block_height_event_data, project_id
                )
            # send commit ID confirmation callback
            if cb_url:
                await send_commit_callback(
                    aiohttp_session=app.aiohttp_client_session,
                    url=cb_url,
                    payload={
                        'commitID': event_data['event_data']['payloadCommitId'],
                        'projectID': project_id,
                        'status': True
                    }
                )

            # --- pending DAG block creations once current tx confirmation has inserted its corresponding DAG block ---
            # get txs from higher heights which have received a confirmation callback
            # and form a continous sequence
            # hence, are ready to be added to chain
            all_pending_txs = await reader_redis_conn.zrangebyscore(
                name=redis_keys.get_pending_transactions_key(project_id),
                min=tentative_block_height_event_data + 1,
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
                pending_q_fetch_prev_cid_for_dag_block_creation = True
                if _tt_block_height == cur_max_height_project + 1:
                    if _tt_block_height > settings.pruning.segment_size and _tt_block_height % settings.pruning.segment_size == 1:
                        await identify_prune_target(project_id, _tt_block_height)
                        pending_q_fetch_prev_cid_for_dag_block_creation = False
                    custom_logger.info(
                        'Processing queued confirmed tx %s at tentative_block_height: %s',
                        pending_tx_obj, _tt_block_height
                    )
                    async for attempt in AsyncRetrying(
                            reraise=True,
                            # we want to retry as soon since there are enough timed waits in the asyncio awaited operations
                            # also helps with lock not expiring midway while waiting
                            wait=wait_random(min=1, max=2),
                            retry=retry_if_exception(DAGCreationException)
                    ):
                        with attempt:
                            t = attempt.retry_state.attempt_number
                            if t > 1:
                                custom_logger.info(
                                    'Queued DAG block creation %s after successful in-order DAG block creation '
                                    '| Project ID: %s | Tentative Height: %s | Retry attempt: %s',
                                    pending_tx_obj.requestID, project_id, _tt_block_height, t
                                )
                                if not await lock.owned():
                                    # reacquire lock
                                    custom_logger.warning(
                                        'Project specific lock expired while retrying DAG Creation '
                                        '| Project %s | Tentative Height %s | Retry attempt number: %s',
                                        project_id, tentative_block_height_event_data, t
                                    )
                                    r_a = await lock.acquire()
                                    if not r_a:
                                        custom_logger.warning(
                                            'Project specific lock expired while retrying DAG Creation | Failed to reacquire'
                                            '| Project %s | Tentative Height %s | Retry attempt number: %s',
                                            project_id, _tt_block_height, t
                                        )
                                        raise RedisLockAcquisitionFailure

                            """ Create the dag block for this event """
                            _dag_cid, dag_block = await dag_utils.create_dag_block_timebound(
                                tx_hash=pending_tx_obj.event_data.txHash,
                                project_id=project_id,
                                tentative_block_height=_tt_block_height,
                                payload_cid=pending_tx_obj.event_data.snapshotCid,
                                timestamp=int(pending_tx_obj.event_data.timestamp),
                                reader_redis_conn=reader_redis_conn,
                                writer_redis_conn=writer_redis_conn,
                                prev_cid_fetch=pending_q_fetch_prev_cid_for_dag_block_creation,
                                ipfs_write_client=app.ipfs_write_client
                            )
                            custom_logger.info('Created enqueued DAG block with CID %s at height %s', _dag_cid,
                                               _tt_block_height)
                            pending_q_fetch_prev_cid_for_dag_block_creation = True

                    pending_blocks_finalized.append({
                        'status': 'Inserted', 'atHeight': _tt_block_height, 'dagCID': _dag_cid
                    })
                    cur_max_height_project = _tt_block_height
                    # send out diff calculation request
                    if dag_block.prevCid:
                        diff_calculation_request = DiffCalculationRequest(
                            project_id=project_id,
                            dagCid=_dag_cid,
                            lastDagCid=dag_block.prevCid['/'],
                            payloadCid=pending_tx_obj.event_data.snapshotCid,
                            txHash=pending_tx_obj.event_data.txHash,
                            timestamp=pending_tx_obj.event_data.timestamp,
                            tentative_block_height=_tt_block_height
                        )
                        async with app.rmq_channel_pool.acquire() as channel:
                            # to save a call to rabbitmq. we already initialize exchanges and queues beforehand
                            # always ensure exchanges and queues are initialized as part of launch sequence,
                            # not to be checked here
                            exchange = await channel.get_exchange(
                                name=settings.rabbitmq.setup['core']['exchange'],
                                ensure=False
                            )
                            message = Message(
                                diff_calculation_request.json().encode('utf-8'),
                                delivery_mode=DeliveryMode.PERSISTENT,
                            )
                            await exchange.publish(
                                message=message,
                                routing_key='diff-calculation'
                            )
                            custom_logger.debug(
                                'Published diff calculation request | At height %s | Project %s',
                                _tt_block_height, project_id
                            )
                    else:
                        custom_logger.debug(
                            'No diff calculation request to publish for first block | At height %s | Project %s',
                            _tt_block_height, project_id
                        )
                    # clear from pending set
                    _ = await writer_redis_conn.zrem(
                        redis_keys.get_pending_transactions_key(project_id),
                        pending_tx_entry
                    )
                    if _:
                        custom_logger.debug(
                            'Cleared entry for requestID %s from pending set at block height %s | project ID %s',
                            pending_tx_obj.requestID, _tt_block_height, project_id
                        )
                    else:
                        custom_logger.debug(
                            'Not sure if entry cleared for requestID %s from pending set at block height %s | '
                            'Redis return: %s | Project ID : %s',
                            pending_tx_obj.requestID, _tt_block_height, _, project_id
                        )
                    # send commit ID confirmation callback
                    if cb_url:
                        await send_commit_callback(
                            aiohttp_session=app.aiohttp_client_session,
                            url=cb_url,
                            payload={
                                'commitID': pending_tx_obj.event_data.payloadCommitId,
                                'projectID': project_id,
                                'status': True
                            }
                        )

    # release aioredis lock
    try:
        await lock.release()
    except aioredis.exceptions.LockNotOwnedError:
        custom_logger.error(
            'Error releasing lock for project ID %s since lock not owned by current task',
            project_id
        )


async def identify_prune_target(project_id, tentative_max_height):
    # should be triggered when finalized_height (mod) segment_size == 1
    writer_redis_conn: aioredis.Redis = app.writer_redis_pool
    reader_redis_conn: aioredis.Redis = app.reader_redis_pool
    # if dagSegments are not present, initialize for the entire chain
    # first run, do not divide between height 1 -> cur_max
    # back up entire segment, 1 - 2800, for example with expected segment length, 700
    # insertion of DAG block at 2801, should contain prevCid = null right there and not refer to 2800
    # next run, qualified target would be 2801 - 3500, and would be triggered at height 3501

    # store
    p_ = await reader_redis_conn.hgetall(
        name=redis_keys.get_project_dag_segments_key(project_id)
    )
    try:
        # get the DAG CID at tentative_max_height - 1 to set the endDAGCid field against the chain segment
        # in project state metadata
        future_dag_cid = helper_functions.get_dag_cid(
            project_id=project_id,
            block_height=tentative_max_height - 1,
            reader_redis_conn=reader_redis_conn
        )
        last_dag_cid = await asyncio.wait_for(
            future_dag_cid,
            # 80% of half life to account for worst case where delay is increased and subseq operations need to complete
            timeout=settings.webhook_listener.redis_lock_lifetime / 2 * 0.8
        )
    except Exception as e:
        rest_logger.error(
            "Failure while getting dag cid from Redis zset of project CIDs, Exception: %s", e, exc_info=True
        )
    else:
        begin_height = 1
        if len(p_) > 0:
            sorted_keys = sorted(list(p_.keys()), key = lambda x: (len(x),x))
            sorted_dict = {k:p_[k] for k in sorted_keys}
            last_dag_segment = ProjectDAGChainSegmentMetadata.parse_raw(sorted_dict[sorted_keys[-1]])
            begin_height = last_dag_segment.endHeight+1
        new_project_dag_segment = ProjectDAGChainSegmentMetadata(
            beginHeight=begin_height,
            endHeight=tentative_max_height - 1,
            endDAGCID=last_dag_cid,
            storageType='pending'
        )
        await writer_redis_conn.hset(redis_keys.get_project_dag_segments_key(project_id),new_project_dag_segment.endHeight,new_project_dag_segment.json())


@app.post('/')
async def create_dag(
        request: Request,
        response: Response,
        x_hook_signature: str = Header(None),
):
    # global project_specific_locks_multiprocessing_map
    global project_specific_locks_multiprocessing_map
    event_data = await request.json()
    response_body = dict()
    project_specific_locks_multiprocessing_map[os.getpid()] = event_data
    rest_logger.debug('Updated global dict: %s', project_specific_locks_multiprocessing_map)
    response_status_code = 200
    # Verify the payload that has arrived.
    if x_hook_signature and settings.webhook_listener.validate_header_sig:
        is_safe = dag_utils.check_signature(event_data, x_hook_signature)
        if is_safe:
            rest_logger.debug("The arriving payload has been verified")
            pass
        else:
            rest_logger.debug("Received wrong signature payload")
            response.status_code = response_status_code
            response_body = {'status': 'BadXHookSignature'}
            return response_body
    if 'event_name' in event_data.keys():
        if event_data['event_name'] == 'RecordAppended':
            rest_logger.debug(event_data)
            try:
                asyncio.ensure_future(payload_to_dag_processor_task(event_data))
            except DAGCreationException:
                response_status_code = 500

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
