from pygate_grpc.client import PowerGateClient
import logging
import sys
from config import settings
import asyncio
import json
import time
from bloom_filter import BloomFilter
from ipfs_async import client as ipfs_client
from redis_conn import provide_async_writer_conn_inst, provide_async_reader_conn_inst
from utils import preprocess_dag

""" Inititalize the logger """
retrieval_logger = logging.getLogger(__name__)
retrieval_logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(levelname)-8s %(name)-4s %(asctime)s %(msecs)d %(module)s-%(funcName)s: %(message)s")
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
retrieval_logger.addHandler(stream_handler)
retrieval_logger.debug("Initialized logger")

""" aioredis boilerplate """
reader_redis_pool = None
writer_redis_pool = None

REDIS_WRITER_CONN_CONF = {
    "host": settings['REDIS']['HOST'],
    "port": settings['REDIS']['PORT'],
    "password": settings['REDIS']['PASSWORD'],
    "db": settings['REDIS']['DB']
}

REDIS_READER_CONN_CONF = {
    "host": settings['REDIS_READER']['HOST'],
    "port": settings['REDIS_READER']['PORT'],
    "password": settings['REDIS_READER']['PASSWORD'],
    "db": settings['REDIS_READER']['DB']
}


@provide_async_writer_conn_inst
@provide_async_reader_conn_inst
async def retrieve_files(reader_redis_conn=None, writer_redis_conn=None):
    powgate_client = PowerGateClient("127.0.0.1:5002", False)

    """ Get all the pending requests """
    requests_list_key = f"pendingRetrievalRequests"
    all_requests = await reader_redis_conn.smembers(requests_list_key)
    if all_requests:
        for requestId in all_requests:
            requestId = requestId.decode('utf-8')
            retrieval_logger.debug("Processing request: " + requestId)

            """ Get the required information about the requestId """
            key = f"retrievalRequestInfo:{requestId}"
            out = await reader_redis_conn.hgetall(key=key)
            request_info = {k.decode('utf-8'): i.decode('utf-8') for k, i in out.items()}
            retrieval_logger.debug(f"Retrieved information for request: {requestId}")
            retrieval_logger.debug(request_info)

            """ Check if any of the files in this request are not pushed to filecoin yet """
            # Get the height of last pruned cid
            last_pruned_key = f"lastPruned:{request_info['projectId']}"
            out: bytes = await reader_redis_conn.get(last_pruned_key)
            if out:
                last_pruned_height = int(out.decode('utf-8'))
            else:
                _ = await writer_redis_conn.set(last_pruned_key, 0)
                last_pruned_height = 0

            retrieval_logger.debug("Last Pruned Height:")
            retrieval_logger.debug(last_pruned_height)

            # Get the token for that projectId
            token_key = f"filecoinToken:{request_info['projectId']}"
            token = await reader_redis_conn.get(token_key)
            token = token.decode('utf-8')

            """ 
                - For the project_id, using the from_height and to_height from the request_info, 
                get all the DAG block cids. 
            """
            block_cids_key = f"projectID:{request_info['projectId']}:Cids"
            all_cids = await reader_redis_conn.zrangebyscore(
                key=block_cids_key,
                max=int(request_info['to_height']),
                min=int(request_info['from_height']),
                withscores=True
            )

            block_height_key = f"projectID:{request_info['projectId']}:blockHeight"
            max_block_height = await reader_redis_conn.get(block_height_key)
            max_block_height = int(max_block_height)

            # Iterate through each dag block
            for block_cid, block_height in all_cids:
                block_cid = block_cid.decode('utf-8')
                block_height = int(block_height)
                block_dag = None
                payload_data = None

                """ Check if the DAG block is pinned """
                if (block_height < max_block_height - settings.max_ipfs_blocks) or (
                        last_pruned_height < int(request_info['to_height'])):
                    """ Get the data directly through the IPFS client """
                    _block_dag = await ipfs_client.dag.get(block_cid)
                    block_dag = _block_dag.as_json()
                    block_dag = preprocess_dag(block_dag)

                    payload_data = await ipfs_client.cat(block_dag['data']['cid'])
                    if isinstance(payload_data, bytes):
                        payload_data = payload_data.decode('utf-8')

                else:
                    """ Get the container for that block height """
                    containers_created_key = f"projectID:{request_info['projectId']}:containers"
                    target_containers = await reader_redis_conn.zrangebyscore(
                        key=containers_created_key,
                        max=settings.container_height * 2 + block_height + 1,
                        min=block_height - settings.container_height * 2 - 1
                    )

                    """ Iterate through each containerId and then check if the block exists in that container """
                    bloom_object = None
                    container_data = None
                    for container_id in target_containers:
                        """ Get the data for the container """
                        container_id = container_id.decode('utf-8')
                        container_data_key = f"projectID:{request_info['projectId']}:containerData:{container_id}"
                        out = await reader_redis_conn.hgetall(container_data_key)
                        container_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in out.items()}
                        bloom_filter_settings = json.loads(container_data['bloomFilterSettings'])
                        retrieval_logger.debug(bloom_filter_settings)
                        bloom_object = BloomFilter(**bloom_filter_settings)
                        if block_cid in bloom_object:
                            break

                    retrieval_logger.debug("Found the matching container")
                    retrieval_logger.debug(container_data)

                    out = powgate_client.data.get(container_data['containerCid'], token=token).decode('utf-8')
                    container = json.loads(out)['container']
                    _block_index = block_height % settings.container_height
                    block_cid, block_dag = next(iter(container['dagChain'][_block_index].items()))
                    payload_data = container['payloads'][block_dag['data']['cid']]

                retrieval_logger.debug("Retrieved the DAG Block...")

                retrieval_data = {}

                if request_info['data'] == '2':
                    """ Save only the payload data """
                    payload_cid = block_dag['data']['cid']
                    # Get payload from filecoin
                    payload = payload_data

                    retrieval_data['cid'] = payload_cid
                    retrieval_data['type'] = 'COLD_FILECOIN'
                    retrieval_data['payload'] = payload

                elif int(request_info['data']) in range(0, 2):  # Either 0 or 1
                    """ Save the entire DAG Block """
                    retrieval_data = {k: v for k, v in block_dag.items()}

                    if request_info['data'] == '1':
                        """ Save the payload data in the DAG block """
                        retrieval_data['data']['payload'] = payload_data

                else:
                    raise ValueError(f"Invalid value for data field in request_info: {request_info['data']}")

                # Save the required retrieval data 
                block_file_path = f'static/{block_cid}'
                with open(block_file_path, 'w') as f:
                    f.write(json.dumps(retrieval_data))

                retrieval_files_key = f"retrievalRequestFiles:{requestId}"
                _ = await writer_redis_conn.zadd(
                    key=retrieval_files_key,
                    member=block_file_path,
                    score=int(block_height)
                )
                retrieval_logger.debug(f"Block {block_cid} is saved")

            # Once the request is complete, then delete the request id from pending retrieval requests set
            _ = await writer_redis_conn.srem(requests_list_key, requestId)
            retrieval_logger.debug(f"Request: {requestId} has been removed from pending retrieveal requests")
    else:
        retrieval_logger.debug(f"No pending requests found....")


if __name__ == "__main__":
    while True:
        retrieval_logger.debug("Looking for pending retrieval requests....")
        asyncio.run(retrieve_files())
        retrieval_logger.debug("Sleeping for 20 secs")
        time.sleep(settings.retrieval_service_interval)