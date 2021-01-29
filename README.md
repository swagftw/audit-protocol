# audit-protocol-private

```
make localnet

python3 gunicorn_main_launcher.py
python3 gunicorn_webhook_launcher.py
python3 payload_commit_service.py
python3 retrieval_service.py
python3 pruning_service.py

```

#### Options
- "max_ipfs_blocks": This number represents the latest max_ipfs_blocks need to fetched using ipfs_client
- "max_pending_payload_commits": This variable is not yet used anywhere. This variable represents the limit at which, 
you need to block further calls to the commit_payload endpoint. Once the pending_payload_commits Queue reach a certain 
limit, it needs to be alerted.
- "block_storage": Either IPFS to FILECOIN. This variable represents where each of the block or payload needs to be stored
- "payload_storage": Same as that of the block_storage
- "container_height": The no.of DAG blocks each container needs to hold.
- "backup_target": Defaults to FILECOIN for now. This variable represents where we want to backup the containers


# Powerloom-protocol

Audit data by snapshotting and generating proof through a smart contract. 

Run the docker:
```
cd docker/
make powerloom
```

Try to commit some payload:
```shell
curl --location --request POST 'http://127.0.0.1:9000/commit_payload' \
--header 'Content-Type: text/plain' \
--data-raw '{
  "payload": {
      "test_field_a": "put any kind data here",
      "test_field_b": {
          "key_a": [1, 2],
          "key_b": 5000
      }
  },
  "projectId": "test_project_1"
}

```
The response you get should be:
```shell
{
    "cid": "QmQa7YZLitKkcMwRmZnx93wSEYjmtUxZgfdMJ1TQwSxgDa",
    "tentativeHeight": 1,
    "payloadChanged": true
}
```

The are 3 fields in the above body:

- cid: This represents the content-identifier for the payload committed. It is a unique
hash for the payload field.
  
- tentativeHeight: Every new payload committed will have height that represents its position
in the chain of payloads committed. However tentativeHeight is not a deterministic value. If
  the proof for this payload is not recieved in the backend, the payload is discarded.
  
- payloadChanged: Represents whether the payload has changed from previous commit or not