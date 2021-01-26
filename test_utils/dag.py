from ipfs_async import client as ipfs_client
import async_timeout
from dynaconf import settings
import asyncio
import json
import io
import helper_functions
from redis_conn import provide_async_reader_conn_inst, provide_async_writer_conn_inst
import redis_keys
import logging
import aioredis

logger = logging.getLogger(__name__)
logger.setLevel(level="DEBUG")


async def get_dag_block(dag_cid: str):
    e_obj = None
    try:
        async with async_timeout.timeout(settings.IPFS_TIMEOUT) as cm:
            try:
                dag = await ipfs_client.dag.get(dag_cid)
            except Exception as e:
                e_obj = e
    except (asyncio.exceptions.CancelledError, asyncio.exceptions.TimeoutError) as err:
        e_obj = err

    if e_obj or cm.expired:
        return {}

    return dag.as_json()


async def put_dag_block(dag_data: dict):
    try:
        dag_json = json.dumps(dag_data)
    except TypeError as terr:
        logger.debug(terr)
        return -1
    else:
        dag_json = dag_json.encode('utf-8')

    out = await ipfs_client.dag.put(io.BytesIO(dag_json))
    dag_cid = out.as_json()['Cid']['/']

    return dag_cid


@provide_async_reader_conn_inst
@provide_async_writer_conn_inst
async def create_dag_block(
        tx_hash: str,
        project_id: str,
        tentative_block_height: int,
        payload_cid: str,
        timestamp: int,
        reader_redis_conn,
        writer_redis_conn,
):
    """ Get the last dag cid using the tentativeBlockHeight"""
    last_dag_cid = await helper_functions.get_dag_cid(
        project_id=project_id,
        block_height=tentative_block_height - 1,
        reader_redis_conn=reader_redis_conn
    )

    """ Fill up the dag """
    dag = settings.dag_structure
    dag['height'] = tentative_block_height
    dag['prevCid'] = last_dag_cid
    dag['data'] = {
        'cid': payload_cid,
        'type': 'HOT_IPFS',
    }
    dag['txHash'] = tx_hash
    dag['timestamp'] = timestamp

    logger.debug("DAG created: ")
    logger.debug(dag)

    """ Convert dag structure to json and put it on ipfs dag """
    dag_cid = await put_dag_block(dag)
    if dag_cid == -1:
        logger.debug("Failed to put dag block on ipfs.")
        return -1

    """ Update redis keys """
    last_dag_cid_key = redis_keys.get_last_dag_cid_key(project_id)
    _ = await writer_redis_conn.set(last_dag_cid_key, dag_cid)

    _ = await writer_redis_conn.zadd(
        key=redis_keys.get_dag_cids_key(project_id),
        score=tentative_block_height,
        member=dag_cid
    )

    block_height_key = redis_keys.get_block_height_key(project_id=project_id)
    _ = await writer_redis_conn.set(block_height_key, tentative_block_height)

    return dag_cid, dag


@provide_async_writer_conn_inst
async def discard_event(
        project_id: str,
        payload_commit_id: str,
        payload_cid: str,
        tx_hash: str,
        tentative_block_height: int,
        writer_redis_conn: aioredis.Redis
):
    redis_output = []
    d_r = await clear_payload_commit_data(
        project_id=project_id,
        payload_commit_id=payload_commit_id,
        tx_hash=tx_hash
    )
    redis_output.extend(d_r)

    # Delete the payload cid from the list of payloadCids
    # out = await writer_redis_conn.zrem(
    #     key=redis_keys.get_payload_cids_key(project_id),
    #     member=payload_cid
    # )
    # redis_output.append(out)

    # Add the transaction Hash to discarded Transactions
    out = await writer_redis_conn.zadd(
        key=redis_keys.get_discarded_transactions_key(project_id),
        member=tx_hash,
        score=tentative_block_height
    )
    redis_output.append(out)

    return redis_output


@provide_async_writer_conn_inst
async def clear_payload_commit_data(
        project_id: str,
        payload_commit_id: str,
        tx_hash: str,
        writer_redis_conn: aioredis.Redis
):
    """
    This function will be called once a dag block creation is successful to
    clear up all the transient, temporary redis keys associated with that
    particular dag block, since these key will not be needed anymore
    once the dag block has been created successfully.
        - Clear Event Data
        - Remove the tx_hash from pendingTransactions key
        - Remove the payload_commit_id from pendingBlocks
        - Delete the payload_commit_data
    """
    deletion_result = []

    # Delete the event data
    out = await writer_redis_conn.delete(
        key=redis_keys.get_event_data_key(payload_commit_id=payload_commit_id)
    )
    deletion_result.append(out)

    # remove tx_hash from list of pending transactions
    out = await writer_redis_conn.zrem(
        key=redis_keys.get_pending_transactions_key(project_id=project_id),
        member=tx_hash
    )
    deletion_result.append(out)

    # remove the payload commit id from the list of pending blocks
    out = await writer_redis_conn.zrem(
        key=redis_keys.get_pending_blocks_key(project_id=project_id),
        member=payload_commit_id
    )
    deletion_result.append(out)

    # delete the payload commit id data
    out = await writer_redis_conn.delete(
        key=redis_keys.get_payload_commit_key(payload_commit_id=payload_commit_id)
    )
    deletion_result.append(out)

    return deletion_result

