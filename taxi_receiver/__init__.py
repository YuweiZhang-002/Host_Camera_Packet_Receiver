"""
taxi_receiver
=============

Layered re-implementation of the original single-file TAXI/0x88B5
Ethernet receiver prototype. See README.md for the layer breakdown.

Layer map (-> module):
    1. Capture              -> capture.py
    2. Ethernet Validation   -> eth_validate.py     (Stage: stages.ValidationStage)
    3. Camera Packet Parser  -> packet_format.py + camera_parser.py  (Stage: stages.ParsingStage)
    4. Stream Monitor        -> stream_monitor.py   (Stage: stages.MonitoringStage)
    5. Frame Reassembler     -> reassembler.py      (Stage: stages.ReassemblyStage)
       Atomic Archive        -> storage.py          (StorageAndPipeline callback)
    6. Threshold Recover     -> threshold_recover.py (on_completed_frame business callback)
       Numbered image/CSV     -> image_pipeline.py    (completed-frame + per-packet callbacks)

    archive adapter          -> archive_layout.py
    archive monitor          -> archive_monitor.py
    viewer UI                -> camera_viewer.py / viewer_cli.py
    demo producer            -> demo_archive_producer.py

    chain composition         -> stages.py   (build_stage_chain: pick Layer1-2/3/4/5 by name)
    orchestration              -> pipeline.py  (queue + worker thread, runs whatever chain it's given)
    CLI                        -> cli.py       (--max-stage {validate,parse,monitor,reassemble})
    pcap / error-frame I/O      -> recorder.py
"""
