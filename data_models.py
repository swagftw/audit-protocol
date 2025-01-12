from pydantic import BaseModel, validator
from typing import Union, List, Optional, Any, Dict
import json
from enum import Enum


class ProjectDAGChainSegmentMetadata(BaseModel):
    beginHeight: int
    endHeight: int
    endDAGCID: str
    storageType: str


class ProjectStateMetadata(BaseModel):
    projectID: str
    dagChains: List[ProjectDAGChainSegmentMetadata]

class SourceChainDetails(BaseModel):
    chainID: int
    epochStartHeight: int
    epochEndHeight: int

class AuditRecordTxEventData(BaseModel):
    txHash: str
    projectId: str
    apiKeyHash: str
    timestamp: float
    payloadCommitId: str
    snapshotCid: Optional[str]
    tentativeBlockHeight: int
    skipAnchorProof: bool = True


class PendingTransaction(BaseModel):
    txHash: str
    requestID: str
    lastTouchedBlock: int = 0
    event_data: Optional[AuditRecordTxEventData] = dict()


class PayloadCommitAPIRequest(BaseModel):
    projectId: str
    payload: dict
    web3Storage: bool = False
    # skip anchor tx by default, unless passed
    skipAnchorProof: bool = True
    sourceChainDetails: Optional[SourceChainDetails] = None
    requestID: Optional[str] = None


class PayloadCommit(BaseModel):
    projectId: str
    commitId: str
    payload: Optional[dict] = None
    requestID: Optional[str] = None
    # following two can be used to substitute for not supplying the payload but the CID and hash itself
    snapshotCID: Optional[str] = None
    apiKeyHash: Optional[str] = None
    tentativeBlockHeight: int = 0
    resubmitted: bool = False
    resubmissionBlock: int = 0  # corresponds to lastTouchedBlock in PendingTransaction model
    web3Storage: bool = False
    skipAnchorProof: bool = True
    sourceChainDetails: Optional[SourceChainDetails] = None

class DAGBlockRange(BaseModel):
    head_block_cid: str
    tail_block_cid: str

class liquidityProcessedData(BaseModel):
    contractAddress: str
    name: str
    liquidity: str
    volume_24h: str
    volume_7d: str
    cid_volume_24h: DAGBlockRange
    cid_volume_7d: DAGBlockRange
    fees_24h: str
    block_height: int
    block_timestamp: int
    token0Liquidity: float
    token1Liquidity: float
    token0LiquidityUSD: float
    token1LiquidityUSD: float
    token0TradeVolume_24h: float
    token1TradeVolume_24h: float
    token0TradeVolumeUSD_24h: float
    token1TradeVolumeUSD_24h: float
    token0TradeVolume_7d: float
    token1TradeVolume_7d: float
    token0TradeVolumeUSD_7d: float
    token1TradeVolumeUSD_7d: float


class DAGBlockPayloadLinkedPath(BaseModel):
    cid: Dict[str, str]


class DAGBlock(BaseModel):
    height: int
    prevCid: Optional[Dict[str, str]]
    prevRoot: Optional[str] = None
    data: Optional[DAGBlockPayloadLinkedPath]
    txHash: str
    timestamp: int


class DAGFinalizerCBEventData(BaseModel):
    apiKeyHash: str
    tentativeBlockHeight: int
    projectId: str
    snapshotCid: Optional[str]
    payloadCommitId: str
    timestamp: int


class DAGFinalizerCallback(BaseModel):
    txHash: str
    requestID: str
    event_data: DAGFinalizerCBEventData


class uniswapPairsSnapshotZset(BaseModel):
    cid: str
    txHash: str = None
    begin_block_height_24h: int
    begin_block_timestamp_24h: int
    begin_block_height_7d: int
    begin_block_timestamp_7d: int
    txStatus: int
    dagHeight: int
    prevTxHash: str = None


class uniswapDailyStatsSnapshotZset(BaseModel):
    cid: str
    txHash: str = None
    txStatus: int
    dagHeight: int
    prevTxHash: str = None

class PairLiquidity(BaseModel):
    total_liquidity: float = 0.0
    token0_liquidity: float = 0.0
    token1_liquidity: float = 0.0
    token0_liquidity_usd: float = 0.0
    token1_liquidity_usd: float = 0.0
    block_height_total_reserve: int = 0
    block_timestamp_total_reserve: int = 0


class PairTradeVolume(BaseModel):
    total_volume: int = 0
    fees: int = 0
    token0_volume: int = 0
    token1_volume: int = 0
    token0_volume_usd: int = 0
    token1_volume_usd: int = 0


class ProjectBlockHeightStatus(BaseModel):
    project_id: str
    block_height: int
    payload_cid: Optional[str] = None
    tx_hash: Optional[str] = None
    status: int = 1 #BLOCK_STATUS_SNAPSHOT_COMMIT_PENDING


class SnapshotterIssueSeverity(str, Enum):
    high = 'HIGH'
    medium = 'MEDIUM'
    low = 'LOW'
    cleared = 'CLEARED'


class SnapshotterIssueType(str, Enum):
    snapshotting_fallen_behind = 'SNAPSHOTTING_FALLEN_BEHIND'
    missed_snapshot = 'MISSED_SNAPSHOT'
    infra_issue = 'INFRA_ISSUE'
    skip_epoch = 'SKIP_EPOCH'
    dag_chain_stuck = 'DAG_CHAIN_STUCK'
    pruning_failed = 'PRUNING_FAILED'


class SnapshotterIssue(BaseModel):
    instanceID: str
    namespace: Optional[str]
    severity: SnapshotterIssueSeverity
    issueType: str
    projectID: str
    epochs: Optional[List[int]]
    timeOfReporting: int
    noOfEpochsBehind: Optional[int]
    extra: Optional[dict]
    serviceName: str


class PeerRegistrationRequest(BaseModel):
    projectID: str
    instanceID: str

class ProjectRegistrationRequest(BaseModel):
    projectIDs: List[str]


class IndexingRegistrationData(BaseModel):
    projectID: str
    indexerConfig: Dict

class ProjectRegistrationRequestForIndexing(BaseModel):
    projects: List[IndexingRegistrationData]
    namespace: str


class EpochConsensusStatus(str, Enum):
    consensus_achieved = 'CONSENSUS_ACHIEVED'
    no_consensus = 'NO_CONSENSUS'


class SubmissionAcceptanceStatus(str, Enum):
    accepted = 'ACCEPTED'
    finalized = 'FINALIZED'
    # if the peer never submitted yet comes around checking for status, trying to work around the system
    notsubmitted = 'NOTSUBMITTED'
    # if all peers have submitted their snapshots and 2/3 consensus has not been reached
    # if submission deadline has passed, all peers have not submitted and 2/3 not reached
    indeterminate = 'INDETERMINATE'


class SubmissionResponse(BaseModel):
    status: Union[SubmissionAcceptanceStatus, EpochConsensusStatus]
    delayedSubmission: bool
    finalizedSnapshotCID: Optional[str] = None


class EpochBase(BaseModel):
    begin: int
    end: int


class SnapshotBase(BaseModel):
    epoch: EpochBase
    projectID: str
    instanceID: str


class SnapshotSubmission(SnapshotBase):
    snapshotCID: str


class SubmissionDataStoreEntry(BaseModel):
    snapshotCID: str
    submittedTS: int
