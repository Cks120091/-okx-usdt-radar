# Compact preflight / single-coin scan and history completion notification

- The 15m/4H preflight page keeps the execution verdict, current position/remaining R:R, frozen Entry/SL/TP plan, and 15m Trigger history rate in the read-first layer. Execution quality, OI/CVD, continuation and data-source details are grouped behind one disclosure.
- The single-coin scan dialog keeps the latest decision and trade plan first. Flow/research detail is grouped behind one disclosure, and the recursive single-scan button inside the result is hidden.
- History completion notification is a separate browser preference from market-scan completion notifications.
- The current notification implementation watches the explicit history job while the history page/PWA remains alive, including background-tab polling. It does not claim guaranteed delivery after the browser/PWA process has been force-closed; that would require a durable server-side push registration for the history job.
- No trading strategy, Entry/SL/TP generation, risk gate, ranking, OI/CVD decision logic or Signal Episode behavior is changed by this UI pass.
