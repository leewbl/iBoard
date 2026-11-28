---- MODULE CommandLane ----
EXTENDS Naturals

CONSTANTS MaxCommands, Capacity, AllowOverlap
VARIABLES submitted, waiting, active, settled, lastResult

vars == <<submitted, waiting, active, settled, lastResult>>
admissionLimit == IF AllowOverlap THEN Capacity + 1 ELSE Capacity

Init ==
    /\ submitted = 0
    /\ waiting = 0
    /\ active = 0
    /\ settled = 0
    /\ lastResult = "none"

Enqueue ==
    /\ submitted < MaxCommands
    /\ submitted' = submitted + 1
    /\ IF active < admissionLimit
          THEN /\ active' = active + 1
               /\ UNCHANGED waiting
          ELSE /\ waiting' = waiting + 1
               /\ UNCHANGED active
    /\ UNCHANGED <<settled, lastResult>>

Settle(result) ==
    /\ active > 0
    /\ settled' = settled + 1
    /\ lastResult' = result
    /\ IF waiting > 0
          THEN /\ waiting' = waiting - 1
               /\ UNCHANGED active
          ELSE /\ active' = active - 1
               /\ UNCHANGED waiting
    /\ UNCHANGED submitted

Resolve == Settle("fulfilled")
Reject == Settle("rejected")
Done == /\ settled = MaxCommands
        /\ UNCHANGED vars

Next == Enqueue \/ Resolve \/ Reject \/ Done

SingleActive == active <= Capacity
Conservation == submitted = waiting + active + settled
====
