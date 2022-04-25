package main

import "encoding/json"

type RecordTxEventData struct {
	TxHash               string  `json:"txHash"`
	ProjectId            string  `json:"projectId"`
	ApiKeyHash           string  `json:"apiKeyHash"`
	Timestamp            float64 `json:"timestamp"`
	PayloadCommitId      string  `json:"payloadCommitId"`
	SnapshotCid          string  `json:"snapshotCid"`
	TentativeBlockHeight int     `json:"tentativeBlockHeight"`
}

type PendingTransaction struct {
	TxHash           string            `json:"txHash"`
	LastTouchedBlock int               `json:"lastTouchedBlock"`
	EventData        RecordTxEventData `json:"event_data"`
}

type PayloadCommit struct {
	ProjectId string `json:"projectId"`
	CommitId  string `json:"commitId"`
	Payload   json.RawMessage
	// following two can be used to substitute for not supplying the payload but the CID and hash itself
	SnapshotCID          string `json:"snapshotCID"`
	ApiKeyHash           string `json:"apiKeyHash"`
	TentativeBlockHeight int    `json:"tentativeBlockHeight"`
	Resubmitted          bool   `json:"resubmitted"`
	ResubmissionBlock    int    `json:"resubmissionBlock"` // corresponds to lastTouchedBlock in PendingTransaction model
}

type Snapshot struct {
	Cid  string `json:"cid"`
	Type string `json:"type"`
}

type CommonVigilRequestParams struct {
	Contract          string          `json:"contract"`
	Method            string          `json:"method"`
	Params            json.RawMessage `json:"params"`
	NetworkId         int             `json:"networkid"`
	Proxy             string          `json:"proxy"` //Review type
	HackerMan         bool            `json:"hackerman"`
	IgnoreGasEstimate bool            `json:"ignoreGasEstimate"`
}

type AuditContractCommitParams struct {
	PayloadCommitId      string `json:"payloadCommitId"`
	SnapshotCid          string `json:"snapshotCid"`
	ApiKeyHash           string `json:"apiKeyHash"`
	ProjectId            string `json:"projectId"`
	TentativeBlockHeight int    `json:"tentativeBlockHeight"`
}

type AuditContractCommitResp struct {
	Success bool                          `json:"success"`
	Data    []AuditContractCommitRespData `json:"data"`
	Error   AuditContractErrResp          `json:"error"`
}
type AuditContractCommitRespData struct {
	TxHash string `json:"txHash"`
}

type AuditContractErrResp struct {
	Message string `json:"message"`
	Error   struct {
		Message string `json:"message"`
		Details struct {
			BriefMessage string `json:"briefMessage"`
			FullMessage  string `json:"fullMessage"`
			Data         []struct {
				Contract       string          `json:"contract"`
				Method         string          `json:"method"`
				Params         json.RawMessage `json:"params"`
				EncodingErrors struct {
					APIKeyHash string `json:"apiKeyHash"`
				} `json:"encodingErrors"`
			} `json:"data"`
		} `json:"details"`
	} `json:"error"`
}
