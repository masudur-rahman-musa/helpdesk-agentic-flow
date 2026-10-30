# Request lifecycle

One access request, from the employee's sentence to the reply they read. It
takes two chat turns. In the first the agent finds the role and proposes the
request. In the second the employee says yes and the request is filed.

```mermaid
flowchart TD
    msg([" Employee message "]) --> held{"A change waiting<br/>on this chat?"}
    held -- no --> step["Model step with the<br/>tools the roles allow"]
    step -- "asks for lookups" --> look["Lookups through MCP<br/>get_me, list_requestable_roles,<br/>classify_access_request, search_policy"]
    look --> step
    step -- "answers" --> check
    step -- "proposes create_access_request" --> hold["Code checks the arguments,<br/>adds an idempotency key,<br/>saves it and asks for a yes"]
    hold --> ask([" Reply with the summary and a yes or no question "])
    held -- "yes" --> run["create_access_request through MCP"]
    held -- "anything else" --> step
    run --> gate{"Token, kill switch,<br/>RBAC, input check"}
    gate -- refused --> typed["Typed refusal, recorded"]
    gate -- allowed --> decide{"Policy engine decides"}
    decide -- "low risk" --> grant["execute_grant with<br/>an expiry set now"]
    decide -- "needs approval" --> seats["Approver seats saved,<br/>approvers notified"]
    decide -- blocked --> blocked["Blocked, with the<br/>rule cited"]
    grant --> audit["Audit event on the<br/>tenant's hash chain"]
    seats --> audit
    blocked --> audit
    typed --> write
    audit --> write["Answer-only model step"]
    write --> check{"Every number, role and<br/>decision word backed<br/>by a tool result?"}
    check -- "no, at most twice" --> step
    check -- "still no" --> fixed["Code writes the reply<br/>from the results"]
    check -- yes --> reply([" Reply, streamed after its steps "])
    fixed --> reply
```

The decision diamond is the policy engine, not the model. The model reads the
sentence at the top and writes the words at the bottom. The change in between
runs only on the person's yes, and it is checked, decided and recorded by
code.

`search_policy` refuses when the best passage scores below the threshold, and
the turn then offers a ticket instead of an answer from the model's memory.
