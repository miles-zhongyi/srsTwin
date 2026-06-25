# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
"""
Reference descriptions for srsTwin dashboard signaling messages.
Each entry: summary (ladder), purpose, protocol, structure, flow.
"""

MESSAGE_INFO = {
    "ng_setup_request": {
        "summary": "gNB announces supported PLMNs, TAs, and slices to AMF on N2.",
        "purpose": (
            "First NGAP procedure after SCTP association on N2. The gNB (CU-CP) tells the AMF "
            "which PLMNs, tracking areas, and slice support it can serve so the core can route "
            "UE traffic to this node."
        ),
        "protocol": "NGAP over SCTP (N2) · 3GPP TS 38.413 · gNB → AMF",
        "structure": (
            "NGSetupRequest IEs: Global RAN Node ID (gNB ID + PLMN), Supported TA List "
            "(each TA: TAC + broadcast PLMN list + optional slice support), Default Paging DRX, "
            "optional RAN node name. ASN.1 PER-encoded inside NGAP PDU."
        ),
        "flow": (
            "Runs at gNB boot, before any UE attaches. Must complete before Initial UE Message "
            "can be accepted. In srsTwin this happens while the UE container may still be starting."
        ),
    },
    "ng_setup_response": {
        "summary": "AMF accepts gNB and returns served GUAMI / PLMN support.",
        "purpose": (
            "AMF confirms the gNB is known and authorized, returns AMF identity (GUAMI), "
            "served PLMN list, and relative AMF capacity. Establishes the N2 application context."
        ),
        "protocol": "NGAP over SCTP (N2) · 3GPP TS 38.413 · AMF → gNB",
        "structure": (
            "NGSetupResponse IEs: AMF Name, Served GUAMI List, Relative AMF Capacity, "
            "PLMN Support List (PLMN + slice support). Cause=radio-network if rejected."
        ),
        "flow": (
            "Completes N2 setup (~milliseconds). After this the gNB is registered but idle until "
            "a UE performs RRC connection and sends NAS Registration."
        ),
    },
    "ssb_cell_search": {
        "summary": "UE detects SSB, decodes MIB, learns cell PCI and basic parameters.",
        "purpose": (
            "Synchronization and initial access. UE scans for PSS/SSS on the configured band, "
            "locks to an SSB, and reads the MIB in PBCH to obtain system frame number, "
            "SCS, and CORESET#0 / search-space configuration for SIB1."
        ),
        "protocol": "NR PHY · PBCH on SSB · no RRC yet · over ZMQ IQ in srsTwin",
        "structure": (
            "SSB burst: PSS + SSS + PBCH (+ DMRS). MIB (~24 bits) carries: systemFrameNumber (6 MSB), "
            "subCarrierSpacingCommon, ssb-SubcarrierOffset, dmrs-TypeA-Position, "
            "pdcch-ConfigSIB1 (CORESET#0 index, searchSpaceZero), cellBarred, intraFreqReselection."
        ),
        "flow": (
            "First over-the-air step after UE starts. Precedes SIB1 acquisition and RRC connection. "
            "Shown on RU lane because IQ samples pass through ZMQ between srsUE and gNB."
        ),
    },
    "sib1": {
        "summary": "Broadcast system info: PLMN, cell access, SIB scheduling, RACH config.",
        "purpose": (
            "SIB1 tells idle UEs whether the cell is suitable (PLMN match, not barred), "
            "how to acquire other SIBs, and initial RACH/PRACH parameters needed for access."
        ),
        "protocol": "NR RRC · BCCH-DLSCH / SI-RNTI · 3GPP TS 38.331 · gNB → UE",
        "structure": (
            "BCCH-DL-SCH-Message → systemInformationBlockType1: cellSelectionInfo, "
            "cellAccessRelatedInfo (PLMN identity list, TAC, cellIdentity), "
            "si-SchedulingInfo, servingCellConfigCommon (downlink/uplink BWP, "
            "initialDownlinkBWP, prach-ConfigurationIndex, etc.)."
        ),
        "flow": (
            "After MIB/cell search UE camps and reads SIB1. Required before sending RRC Setup Request "
            "on CCCH (Msg3)."
        ),
    },
    "prach_msg1": {
        "summary": "UE transmits random-access preamble on PRACH (Msg1).",
        "purpose": (
            "Contention-based random access. UE selects a preamble and transmits on PRACH so the "
            "gNB can detect access attempt, assign temporary identity (RA-RNTI), and schedule RAR."
        ),
        "protocol": "NR MAC/PHY · PRACH · 3GPP TS 38.321 / 38.211 · UE → gNB via RU",
        "structure": (
            "PRACH occasion: preamble index (from ra-PreambleIndex set in SIB1), "
            "occasion time/frequency from prach-ConfigurationIndex. gNB responds with "
            "RAR (Msg2) on DL-SCH addressed by RA-RNTI."
        ),
        "flow": (
            "Part of 4-step RA: Msg1 (PRACH) → Msg2 (RAR) → Msg3 (RRC Setup Request) → Msg4 (RRC Setup). "
            "Occurs after UE decides to connect and before CCCH uplink."
        ),
    },
    "rar_complete": {
        "summary": "MAC reports RA success; UE has UL grant and TC-RNTI for Msg3.",
        "purpose": (
            "Confirms the UE received Random Access Response, applied timing advance and UL grant, "
            "and can transmit RRC Setup Request on CCCH using the temporary C-RNTI."
        ),
        "protocol": "NR MAC · 3GPP TS 38.321 · internal MAC indication",
        "structure": (
            "RAR PDU (Msg2) contains: TA command, UL grant, Temporary C-RNTI. "
            "Random Access Complete means contention resolution phase can begin after Msg3/Msg4."
        ),
        "flow": (
            "Bridges PHY/MAC random access and RRC connection establishment. "
            "Immediately followed by RRC Setup Request on SRB0/CCCH."
        ),
    },
    "rrc_setup_request": {
        "summary": "Msg3: UE requests RRC connection; carries establishment cause.",
        "purpose": (
            "UE initiates RRC connection on CCCH. Establishes SRB0 and provides "
            "establishmentCause (e.g. mo-Signalling, mo-Data) so gNB can admit the UE."
        ),
        "protocol": "NR RRC · CCCH / SRB0 · UL-DCCH/CCCH · 3GPP TS 38.331 · UE → gNB",
        "structure": (
            "UL-CCCH-Message → rrcSetupRequest: ue-Identity (randomValue or ng-5G-S-TMSI), "
            "establishmentCause, optional spare. Typically 6 bytes in logs."
        ),
        "flow": (
            "Msg3 of random access. gNB answers with RRC Setup (Msg4), assigning C-RNTI and SRB1."
        ),
    },
    "rrc_setup": {
        "summary": "Msg4: gNB assigns C-RNTI, SRB1, and default DRB configuration.",
        "purpose": (
            "Completes RRC connection setup. Provides radioBearerConfig for SRB1, "
            "masterCellGroup with initial BWP, and physical cell configuration so UE "
            "can send RRC Setup Complete with NAS payload."
        ),
        "protocol": "NR RRC · CCCH / SRB0 · DL · 3GPP TS 38.331 · gNB → UE",
        "structure": (
            "DL-CCCH-Message → rrcSetup: radioBearerConfig (SRB1 pdcp/r lc/logical channel), "
            "masterCellGroup (spCellConfig, initial DL/UL BWP, PDCCH/PDSCH/PUSCH configs), "
            "optional measConfig. Large ASN.1 (~297 B in sample attach)."
        ),
        "flow": (
            "Msg4 after RRC Setup Request. UE responds with RRC Setup Complete containing "
            "NAS Registration Request (first NAS over the air)."
        ),
    },
    "rrc_setup_complete": {
        "summary": "UE confirms RRC setup; carries NAS Registration Request to core.",
        "purpose": (
            "Acknowledges RRC Setup and delivers the first NAS message (Registration Request) "
            "inside dedicatedInfoNAS. Triggers gNB to send Initial UE Message on NGAP."
        ),
        "protocol": "NR RRC · SRB1 / DCCH · UL · 3GPP TS 38.331 · UE → gNB",
        "structure": (
            "UL-DCCH-Message → rrcSetupComplete: rrc-TransactionIdentifier, "
            "criticalExtensions → dedicatedNAS-Message (Registration Request 5GMM octets)."
        ),
        "flow": (
            "Ends RRC connection setup phase. gNB extracts NAS and forwards as Initial UE Message "
            "to AMF. This is when Registration actually reaches the 5GC (not the earlier NAS log line)."
        ),
    },
    "nas_registration_request": {
        "summary": "5GMM Registration Request: UE registers with AMF for mobility/CM.",
        "purpose": (
            "UE requests registration type (initial, mobility, periodic), presents SUCI/SUPI "
            "identity, security capabilities, requested NSSAI, and UE status. Starts MM procedure."
        ),
        "protocol": "5G NAS (5GMM) · carried in RRC Setup Complete or UL NAS transport · TS 24.501",
        "structure": (
            "Registration Request: extended protocol discriminator, security header type, "
            "registration type (5GS), ngKSI, 5GS mobile identity (SUCI), optional 5GMM capability, "
            "UE security capability, requested NSSAI, last visited TAI, uplink data status, etc."
        ),
        "flow": (
            "First NAS toward core during attach. srsUE may log 'Sending Registration Request' "
            "before RRC is up—that is local NAS state; over-the-air delivery is in RRC Setup Complete."
        ),
    },
    "initial_ue_message": {
        "summary": "gNB forwards first NAS from UE to AMF with RAN UE NGAP ID.",
        "purpose": (
            "Relays NAS Registration Request from UE to AMF over N2, allocating RAN UE NGAP ID "
            "and including user location info (NRCGI, TAI) and RRC establishment cause."
        ),
        "protocol": "NGAP · 3GPP TS 38.413 · gNB → AMF",
        "structure": (
            "InitialUEMessage IEs: RAN UE NGAP ID, NAS-PDU (Registration Request), "
            "User Location Information (NR-CGI + TAI), RRC Establishment Cause, "
            "optional UE Context Request."
        ),
        "flow": (
            "First per-UE NGAP message. AMF begins authentication and security mode after this."
        ),
    },
    "nas_authentication_request": {
        "summary": "AMF/AUSF challenges UE with RAND, AUTN, ngKSI.",
        "purpose": (
            "Mutual authentication. Network proves knowledge of subscriber key; UE verifies AUTN "
            "and responds with RES* to complete 5G-AKA."
        ),
        "protocol": "5G NAS · 5GMM · typically in DL NAS Transport · TS 24.501",
        "structure": (
            "Authentication Request: ngKSI, ABBA, authentication parameter RAND (16 B), "
            "authentication parameter AUTN (16 B). Wrapped in NGAP DownlinkNASTransport."
        ),
        "flow": (
            "Follows Initial UE Message. UE answers with Authentication Response; failures abort attach."
        ),
    },
    "nas_authentication_response": {
        "summary": "UE returns RES* proving subscription credentials.",
        "purpose": "UE computes authentication response from RAND and USIM key, proving identity to network.",
        "protocol": "5G NAS · 5GMM · UL NAS transport · TS 24.501",
        "structure": (
            "Authentication Response: optional EAP message or authentication response parameter "
            "(RES*, 16 B for 5G-AKA). Sent inside NAS PDU via RRC UL Information Transfer."
        ),
        "flow": "Completes AKA exchange; AMF proceeds to Security Mode Command if successful.",
    },
    "nas_security_mode_command": {
        "summary": "Network selects NAS/RRC algorithms and activates NAS security.",
        "purpose": (
            "AMF instructs UE which ciphering/integrity algorithms to use and establishes "
            "NAS security context (KNASenc/int). All following NAS messages are protected."
        ),
        "protocol": "5G NAS · 5GMM · DL · TS 24.501",
        "structure": (
            "Security Mode Command: selected NAS security algorithms (ciphering + integrity), "
            "ngKSI, optional IMEISV request, replayed UE security capabilities, EAP message if EAP-AKA."
        ),
        "flow": (
            "After authentication. UE confirms with Security Mode Complete; may also appear as "
            "RRC Security Mode Command for AS security separately."
        ),
    },
    "nas_security_mode_complete": {
        "summary": "UE acknowledges NAS security mode; may include IMEISV.",
        "purpose": "Confirms algorithm agreement and activates NAS integrity/ciphering for subsequent NAS.",
        "protocol": "5G NAS · 5GMM · UL · TS 24.501",
        "structure": (
            "Security Mode Complete: optional IMEISV, optional NAS message container. "
            "Protected with new NAS keys after this point."
        ),
        "flow": "Unlocks encrypted NAS (Registration Accept, PDU Session procedures).",
    },
    "security_mode_command": {
        "summary": "RRC activates AS integrity/ciphering (SRB/DRB algorithms).",
        "purpose": (
            "gNB configures Access Stratum security: selects NEA/NIA algorithms, "
            "derives KRRCenc/int, applies security to SRB1 and later DRBs."
        ),
        "protocol": "NR RRC · SRB1 DCCH · DL · TS 38.331",
        "structure": (
            "securityModeCommand: securityConfigSMC (securityAlgorithmConfig: ciphering + integrity), "
            "optional skipped security indicators."
        ),
        "flow": (
            "Parallel to NAS security during attach. UE responds with securityModeComplete before "
            "UE capability enquiry."
        ),
    },
    "security_mode_complete": {
        "summary": "UE confirms AS security activation on SRB1.",
        "purpose": "Acknowledges RRC Security Mode Command; SRB1 traffic becomes integrity-protected/ciphered.",
        "protocol": "NR RRC · SRB1 DCCH · UL · TS 38.331",
        "structure": "securityModeComplete: empty or later criticalExtensions.",
        "flow": "Precedes UE Capability Enquiry and Registration Accept on NAS.",
    },
    "ue_capability_enquiry": {
        "summary": "gNB asks UE for NR/EUTRA capability containers.",
        "purpose": (
            "Collect UE radio capabilities for admission, band combination checks, and "
            "later RRC reconfiguration (DRB, CA, MIMO limits)."
        ),
        "protocol": "NR RRC · SRB1 · DL · TS 38.331",
        "structure": (
            "ueCapabilityEnquiry: ue-CapabilityRAT-RequestList (rat-Type nr/eutra, "
            "capabilityRequestFilter), optional capability enquiry mask."
        ),
        "flow": "During attach before Initial Context Setup; answers drive gNB and AMF decisions.",
    },
    "ue_capability_information": {
        "summary": "UE reports supported bands, MIMO, CA combos, feature sets.",
        "purpose": "Returns UE-NR-Capability and optionally UE-MRDC-Capability for network configuration.",
        "protocol": "NR RRC · SRB1 · UL · TS 38.331",
        "structure": (
            "ueCapabilityInformation: ue-CapabilityRAT-ContainerList with ASN.1 PER capability "
            "containers (often large, hundreds of bytes)."
        ),
        "flow": "Forwarded to AMF via UERadioCapabilityInfoIndication; used for PDU session QoS mapping.",
    },
    "nas_registration_accept": {
        "summary": "AMF accepts UE; assigns 5G-GUTI and registered NSSAI.",
        "purpose": (
            "Completes 5GMM registration. Provides 5G-GUTI, allowed NSSAI, T3512 periodic timer, "
            "and may trigger configuration update."
        ),
        "protocol": "5G NAS · 5GMM · DL · TS 24.501",
        "structure": (
            "Registration Accept: 5GS registration result, 5G-GUTI, equivalent PLMNs, "
            "allowed NSSAI, configured NSSAI, rejected NSSAI, T3512 value, optional PDU session status."
        ),
        "flow": "After security and capabilities. UE responds with Registration Complete.",
    },
    "nas_registration_complete": {
        "summary": "UE acknowledges successful registration.",
        "purpose": "Confirms UE received Registration Accept; MM state becomes REGISTERED.",
        "protocol": "5G NAS · 5GMM · UL · TS 24.501",
        "structure": "Registration Complete: optional SMS indication, optional PDU session status.",
        "flow": "MM registration done; PDU Session Establishment typically follows.",
    },
    "nas_configuration_update_command": {
        "summary": "Network pushes updated GUTI, NSSAI, or MICO parameters.",
        "purpose": (
            "Updates UE configuration without full re-registration (new 5G-GUTI, allowed NSSAI, "
            "LADN, MICO, etc.)."
        ),
        "protocol": "5G NAS · 5GMM · DL · TS 24.501",
        "structure": (
            "Configuration Update Command: optional 5G-GUTI, TAI list, allowed NSSAI, "
            "configured NSSAI, MICO indication, SMS indication."
        ),
        "flow": "Often piggybacked after Registration Accept during initial attach.",
    },
    "nas_configuration_update_complete": {
        "summary": "UE acks configuration update.",
        "purpose": "Confirms UE applied network-provided configuration changes.",
        "protocol": "5G NAS · 5GMM · UL · TS 24.501",
        "structure": "Configuration Update Complete: empty body in most attaches.",
        "flow": "Optional MM step before or in parallel with PDU session setup.",
    },
    "initial_context_setup_request": {
        "summary": "AMF asks gNB to create UE context: security keys, QoS, PDU sessions.",
        "purpose": (
            "Establishes full UE context in gNB: AS security keys, mobility restrictions, "
            "PDU session resources to setup, and UE aggregate maximum bit rate."
        ),
        "protocol": "NGAP · TS 38.413 · AMF → gNB",
        "structure": (
            "InitialContextSetupRequest: AMF UE NGAP ID, GUAMI, Allowed NSSAI, "
            "UE Security Capabilities, Security Key (KgNB), Mobility Restriction List, "
            "PDU Session Resource Setup List (QoS flows, UP TNL info placeholders)."
        ),
        "flow": (
            "After registration accepted. gNB configures SRBs/DRBs and responds with "
            "Initial Context Setup Response + RRC Reconfiguration."
        ),
    },
    "initial_context_setup_response": {
        "summary": "gNB confirms UE context and lists setup PDU session resources.",
        "purpose": (
            "Reports successful establishment of UE context and which PDU sessions / QoS flows "
            "were configured on the radio side."
        ),
        "protocol": "NGAP · TS 38.413 · gNB → AMF",
        "structure": (
            "InitialContextSetupResponse: PDU Session Resource Setup List (response per session), "
            "optional criticality diagnostics."
        ),
        "flow": "Pairs with Initial Context Setup Request; precedes user-plane path setup on N3.",
    },
    "rrc_reconfiguration": {
        "summary": "gNB reconfigures DRBs, measConfig, and cell group for data plane.",
        "purpose": (
            "Adds/modifies DRBs for PDU sessions, applies dedicated bearer config, "
            "measurement gaps, and secondary cell group if needed."
        ),
        "protocol": "NR RRC · SRB1 · DL · TS 38.331",
        "structure": (
            "rrcReconfiguration: radioBearerConfig (DRB setup with PDCP/RLC/LogicalChannel), "
            "masterCellGroup updates, measConfig, dedicatedNAS-Message optional."
        ),
        "flow": (
            "Delivered during PDU session establishment so UE can send/receive user data on DRB."
        ),
    },
    "rrc_reconfiguration_complete": {
        "summary": "UE confirms RRC reconfiguration applied.",
        "purpose": "Acknowledges successful application of DRB and physical config; data radio bearer active.",
        "protocol": "NR RRC · SRB1 · UL · TS 38.331",
        "structure": "rrcReconfigurationComplete: rrc-TransactionIdentifier.",
        "flow": "Completes radio-side PDU session setup; user plane can start on N3/GTP-U.",
    },
    "nas_pdu_session_establishment_request": {
        "summary": "UE requests PDU session: DNN, S-NSSAI, PDU type.",
        "purpose": (
            "SM procedure start. UE asks for PDU session type (IPv4/IPv6/Ethernet), "
            "requested SSC mode, DNN, and S-NSSAI for user-plane connectivity."
        ),
        "protocol": "5G NAS · 5GSM · UL · TS 24.501",
        "structure": (
            "PDU Session Establishment Request: PDU session ID, PTI, integrity protection max data rate, "
            "PDU session type, SSC mode, DNN, S-NSSAI, optional protocol configuration options."
        ),
        "flow": "After MM registered; triggers SMF selection and NGAP PDU Session Resource Setup.",
    },
    "pdu_session_resource_setup_request": {
        "summary": "AMF/SMF requests gNB to allocate radio resources for a PDU session.",
        "purpose": (
            "Maps PDU session QoS flows to DRBs at gNB, provides UL GTP-U tunnel info (N3), "
            "and QoS profile per QFI."
        ),
        "protocol": "NGAP · TS 38.413 · AMF → gNB",
        "structure": (
            "PDUSessionResourceSetupRequest: PDU Session Resource Setup List (PDU session ID, "
            "NAS-PDU, S-NSSAI, QoS Flow Setup List with 5QI/priority/GFBR/MFBR, UL UP transport info)."
        ),
        "flow": (
            "Parallel to SMF session creation. gNB sends RRC Reconfiguration with DRB mapping, "
            "then responds with PDUSessionResourceSetupResponse."
        ),
    },
    "pdu_session_resource_setup_response": {
        "summary": "gNB confirms radio bearer setup for PDU session; returns DL TNL info.",
        "purpose": (
            "Reports setup result per PDU session and provides gNB GTP-U endpoint (N3) "
            "so UPF can forward downlink traffic."
        ),
        "protocol": "NGAP · TS 38.413 · gNB → AMF",
        "structure": (
            "PDUSessionResourceSetupResponse: PDU Session Resource Setup List (DL GTP tunnel, "
            "associated QoS flow list, optional failed setup list)."
        ),
        "flow": "Completes N2 SM signaling leg; UE receives PDU Session Establishment Accept locally.",
    },
    "nas_pdu_session_establishment_accept": {
        "summary": "SMF assigns UE IP, QoS rules, and session parameters.",
        "purpose": (
            "Accepts PDU session; provides PDU address (IPv4/IPv6), QoS rules, "
            "session-AMBR, SSC mode, and always-on indication."
        ),
        "protocol": "5G NAS · 5GSM · DL · TS 24.501",
        "structure": (
            "PDU Session Establishment Accept: selected PDU session type, QoS rules, "
            "session-AMBR, PDU address, S-NSSAI, DNN, optional PCO (DNS, MTU)."
        ),
        "flow": (
            "End of default attach for data. srsTwin logs IP on tun interface (e.g. 10.45.1.x)."
        ),
    },
    "dl_information_transfer": {
        "summary": "RRC carries downlink NAS PDU on SRB2/SRB1.",
        "purpose": "Transparent delivery of DL NAS messages (auth, security mode, SM, etc.) from gNB to UE.",
        "protocol": "NR RRC · SRB1/SRB2 · DL · TS 38.331",
        "structure": (
            "dlInformationTransfer: dedicatedNAS-Message (raw NAS octets). "
            "One NAS message per transfer in most stacks."
        ),
        "flow": "Used whenever AMF sends Downlink NAS Transport during attach and session setup.",
    },
    "ul_information_transfer": {
        "summary": "RRC carries uplink NAS PDU toward core.",
        "purpose": "Transparent delivery of UL NAS (auth response, registration complete, SM requests).",
        "protocol": "NR RRC · SRB1/SRB2 · UL · TS 38.331",
        "structure": "ulInformationTransfer: dedicatedNAS-Message.",
        "flow": "gNB wraps content in Uplink NAS Transport on NGAP.",
    },
    "downlink_nas_transport": {
        "summary": "AMF sends NAS PDU to UE via gNB.",
        "purpose": "Core-to-UE NAS delivery path on N2; carries any DL 5GMM/5GSM message.",
        "protocol": "NGAP · TS 38.413 · AMF → gNB",
        "structure": (
            "DownlinkNASTransport: AMF UE NGAP ID, RAN UE NGAP ID, NAS-PDU, "
            "optional Mobility Restriction List, Index to RFSP."
        ),
        "flow": "gNB typically uses DL Information Transfer on SRB to reach UE.",
    },
    "uplink_nas_transport": {
        "summary": "gNB forwards UE NAS PDU to AMF.",
        "purpose": "UE-to-core NAS relay after AS security is active.",
        "protocol": "NGAP · TS 38.413 · gNB → AMF",
        "structure": (
            "UplinkNASTransport: AMF/RAN UE NGAP ID, NAS-PDU, optional User Location Information."
        ),
        "flow": "Follows UL Information Transfer from UE during attach and SM procedures.",
    },
    "nas_dl_transport": {
        "summary": "Generic DL NAS wrapper logged by srsUE.",
        "purpose": "UE NAS layer received a downlink NAS message (may encapsulate 5GMM/5GSM).",
        "protocol": "5G NAS · TS 24.501",
        "structure": "Outer NAS security header + inner message type (Authentication, Security Mode, SM, etc.).",
        "flow": "Decode inner IE for specific procedure step.",
    },
    "ue_radio_capability_info_indication": {
        "summary": "gNB uploads UE NR capability to AMF.",
        "purpose": "Stores UE radio capability in AMF for paging, handover, and NSA/SA decisions.",
        "protocol": "NGAP · TS 38.413 · gNB → AMF",
        "structure": (
            "UERadioCapabilityInfoIndication: UE Radio Capability (NR container), "
            "optional UE Radio Capability for Paging."
        ),
        "flow": "Sent after UE Capability Information during attach.",
    },
    "ue_context_release_request": {
        "summary": "gNB asks AMF to release UE NGAP context.",
        "purpose": (
            "Indicates RRC release or radio link failure; requests core to release UE context "
            "and PDU sessions."
        ),
        "protocol": "NGAP · TS 38.413 · gNB → AMF",
        "structure": (
            "UEContextReleaseRequest: Cause (radioNetwork, transport, nas, etc.), "
            "PDU Session Resource List (optional)."
        ),
        "flow": "Starts UE detach / release sequence with UE Context Release Command.",
    },
    "ue_context_release_command": {
        "summary": "AMF orders release of UE NGAP and radio resources.",
        "purpose": "Instructs gNB to tear down UE context, DRBs, and NGAP IDs.",
        "protocol": "NGAP · TS 38.413 · AMF → gNB",
        "structure": (
            "UEContextReleaseCommand: UE NGAP IDs, Cause. gNB sends RRC Release to UE."
        ),
        "flow": "End of session or detach; followed by RRC Release on air interface.",
    },
    "rrc_release": {
        "summary": "gNB releases RRC connection; UE returns to IDLE/INACTIVE.",
        "purpose": "Tears down SRBs, stops data radio, may include suspendConfig for RRC INACTIVE.",
        "protocol": "NR RRC · SRB1 · DL · TS 38.331",
        "structure": (
            "rrcRelease: release cause, optional redirectedCarrierInfo, cellReselectionPriorities, "
            "suspendConfig."
        ),
        "flow": "Final air-interface message in detach; UE may reattach later.",
    },
}

# Map dashboard labels (exact or prefix) to catalog keys.
LABEL_RULES = [
    ("NG Setup Request", "ng_setup_request"),
    ("NG Setup Response", "ng_setup_response"),
    ("SSB / PBCH-MIB", "ssb_cell_search"),
    ("SIB1", "sib1"),
    ("PRACH preamble", "prach_msg1"),
    ("Random Access Complete", "rar_complete"),
    ("RRC Setup Request", "rrc_setup_request"),
    ("RRC Setup (Msg4)", "rrc_setup"),
    ("RRC Setup Complete", "rrc_setup_complete"),
    ("NAS: Registration Request", "nas_registration_request"),
    ("Initial UE Message", "initial_ue_message"),
    ("NAS: Authentication Request", "nas_authentication_request"),
    ("NAS: Authentication Response", "nas_authentication_response"),
    ("NAS: Security Mode Command", "nas_security_mode_command"),
    ("NAS: Security Mode Complete", "nas_security_mode_complete"),
    ("Security Mode Command", "security_mode_command"),
    ("Security Mode Complete", "security_mode_complete"),
    ("UE Capability Enquiry", "ue_capability_enquiry"),
    ("UE Capability Information", "ue_capability_information"),
    ("NAS: Registration Accept", "nas_registration_accept"),
    ("NAS: Registration Complete", "nas_registration_complete"),
    ("NAS: Configuration Update Command", "nas_configuration_update_command"),
    ("NAS: Configuration Update Complete", "nas_configuration_update_complete"),
    ("Initial Context Setup Request", "initial_context_setup_request"),
    ("Initial Context Setup Response", "initial_context_setup_response"),
    ("RRC Reconfiguration Complete", "rrc_reconfiguration_complete"),
    ("RRC Reconfiguration", "rrc_reconfiguration"),
    ("NAS: PDU Session Establishment Request", "nas_pdu_session_establishment_request"),
    ("PDU Session Resource Setup Request", "pdu_session_resource_setup_request"),
    ("PDU Session Resource Setup Response", "pdu_session_resource_setup_response"),
    ("PDU Session Establishment Accept", "nas_pdu_session_establishment_accept"),
    ("DL Information Transfer", "dl_information_transfer"),
    ("UL Information Transfer", "ul_information_transfer"),
    ("Downlink NAS Transport", "downlink_nas_transport"),
    ("Uplink NAS Transport", "uplink_nas_transport"),
    ("NAS: DL NAS transport", "nas_dl_transport"),
    ("UE Radio Capability Info Indication", "ue_radio_capability_info_indication"),
    ("UE Context Release Request", "ue_context_release_request"),
    ("UE Context Release Command", "ue_context_release_command"),
    ("RRC Release", "rrc_release"),
]


def lookup_message_info(label: str) -> dict:
    for prefix, key in LABEL_RULES:
        if label.startswith(prefix) or prefix in label:
            info = MESSAGE_INFO.get(key)
            if info:
                return {"key": key, **info}
    return {
        "key": "unknown",
        "summary": "Signaling event parsed from stack logs.",
        "purpose": f"Event: {label}. See log excerpt below for ASN.1/hex detail from srsUE or ocudu.",
        "protocol": "See log layer and route in the header.",
        "structure": "Refer to decoded ASN.1 in the log excerpt (3GPP TS 38.331 / 38.413 / 24.501).",
        "flow": "Part of the 5G SA attach or session procedure shown in the ladder.",
    }


# ---------------------------------------------------------------------------
# 4G LTE catalog — srsRAN_4G attach (RRC + NAS EMM/ESM + S1AP)
# ---------------------------------------------------------------------------
MESSAGE_INFO_4G: dict[str, dict] = {
    "cell_found": {
        "summary": "UE locks PSS/SSS and reads MIB; first DL sync to the cell.",
        "purpose": "Initial cell search. UE finds the strongest cell on the configured EARFCN and decodes the MIB (PBCH) for system bandwidth and SFN before reading SIB1.",
        "protocol": "LTE PHY · PSS/SSS + PBCH · 3GPP TS 36.211/36.213 · over ZMQ IQ in srsTwin",
        "structure": "MIB: system bandwidth, PHICH config, SFN (8 MSB). No RRC yet.",
        "flow": "First over-the-air step; precedes SIB1 acquisition.",
    },
    "rach_tx": {
        "summary": "UE transmits a RACH preamble (MAC-level view of Msg1).",
        "purpose": "Logged by MAC when it hands the selected preamble/PRACH occasion to PHY for transmission.",
        "protocol": "LTE MAC · 3GPP TS 36.321 · UE → eNB",
        "structure": "Preamble index + ra-RNTI derived from the PRACH occasion.",
        "flow": "Same physical event as the PHY 'PRACH preamble (Msg1)' row, logged one layer up.",
    },
    "sib1": {
        "summary": "Broadcast system info: PLMN, TAC, cell access barring, SIB scheduling.",
        "purpose": "Tells idle UEs whether the cell is selectable (PLMN/TAC match, not barred) and how to find SIB2 (RACH config, common channel config).",
        "protocol": "LTE RRC · BCCH-DLSCH on SI-RNTI · 3GPP TS 36.331 · eNB → UE",
        "structure": "SystemInformationBlockType1: cellAccessRelatedInfo (PLMN list, TAC, cellIdentity), cellSelectionInfo, si-SchedulingInfo, freqBandIndicator.",
        "flow": "Read right after MIB; required before the UE can send RRC Connection Request (Msg3).",
    },
    "sib2": {
        "summary": "Common radio config: RACH parameters, PUCCH/PUSCH, paging, timers.",
        "purpose": "Carries radioResourceConfigCommon (PRACH config, power ramping, contention timers) the UE needs to actually perform random access, plus paging and uplink power-control defaults.",
        "protocol": "LTE RRC · BCCH-DLSCH · 3GPP TS 36.331 · eNB → UE",
        "structure": "SystemInformation → sib2: radioResourceConfigCommon, ue-TimersAndConstants, freqInfo, mbsfn-SubframeConfigList (optional).",
        "flow": "Scheduled per si-SchedulingInfo in SIB1; UE must decode it before PRACH.",
    },
    "prach_msg1": {
        "summary": "UE transmits the random-access preamble (Msg1).",
        "purpose": "Contention-based random access: UE picks a preamble from the SIB2 RACH config and transmits it so the eNB can detect the attempt and reply with a Random Access Response.",
        "protocol": "LTE PHY/MAC · PRACH · 3GPP TS 36.321/36.211 · UE → eNB",
        "structure": "PRACH occasion: preamble index, frequency/time resource from prach-ConfigIndex.",
        "flow": "Msg1 of 4-step RA: Msg1 (PRACH) → Msg2 (RAR) → Msg3 (RRC Conn Request) → Msg4 (RRC Conn Setup).",
    },
    "rar_msg2": {
        "summary": "eNB grants UL resources and a temporary C-RNTI (Msg2).",
        "purpose": "Random Access Response: confirms preamble detection, gives the UE a timing-advance command, an uplink grant, and a Temporary C-RNTI to use for Msg3.",
        "protocol": "LTE MAC · DL-SCH addressed by RA-RNTI · 3GPP TS 36.321 · eNB → UE",
        "structure": "RAR PDU: RAPID, Timing Advance Command, UL Grant, Temporary C-RNTI.",
        "flow": "Immediately followed by the UE sending RRC Connection Request on the granted UL resource.",
    },
    "rrc_conn_request": {
        "summary": "Msg3: UE requests an RRC connection on CCCH/SRB0.",
        "purpose": "First RRC message. Carries a random UE identity and an establishment cause (e.g. mo-Signalling for an attach) so the eNB can admit the UE and start the S1AP Initial UE Message toward the MME.",
        "protocol": "LTE RRC · CCCH / SRB0 · UL · 3GPP TS 36.331 · UE → eNB",
        "structure": "RRCConnectionRequest: ue-Identity (randomValue, 40 bits), establishmentCause, spare bit.",
        "flow": "Msg3 of random access; eNB answers with RRC Connection Setup (Msg4) or rejects if the MME/S1 link isn't up.",
    },
    "rrc_conn_setup": {
        "summary": "Msg4: eNB assigns SRB1 and the initial radio bearer config.",
        "purpose": "Completes RRC connection establishment: configures SRB1 (PDCP/RLC/logical channel) so the UE can send RRC Connection Setup Complete, which carries the first NAS message (Attach Request).",
        "protocol": "LTE RRC · CCCH / SRB0 (DL) · 3GPP TS 36.331 · eNB → UE",
        "structure": "RRCConnectionSetup: radioResourceConfigDedicated (srb-ToAddModList for SRB1), rrc-TransactionIdentifier.",
        "flow": "Msg4 after RRC Connection Request; UE responds with RRC Connection Setup Complete carrying NAS Attach Request.",
    },
    "rrc_conn_setup_complete": {
        "summary": "UE confirms RRC setup and piggy-backs the NAS Attach Request.",
        "purpose": "Acknowledges RRC Connection Setup and delivers the dedicatedInfoNAS (Attach Request) to the eNB, which forwards it to the MME as an S1AP Initial UE Message.",
        "protocol": "LTE RRC · SRB1 / DCCH · UL · 3GPP TS 36.331 · UE → eNB",
        "structure": "RRCConnectionSetupComplete: selectedPLMN-Identity, dedicatedInfoNAS (Attach Request octets), rrc-TransactionIdentifier.",
        "flow": "Ends RRC connection establishment; this is when Attach actually reaches the MME, not the earlier NAS log line.",
    },
    "rrc_conn_reject": {
        "summary": "eNB refuses the RRC connection (e.g. MME/S1 not up, overload).",
        "purpose": "Tells the UE to back off and retry later. In srsTwin this fires when the eNB's S1AP link to the MME isn't established yet — every Msg3 gets rejected until the MME comes up.",
        "protocol": "LTE RRC · CCCH / SRB0 (DL) · 3GPP TS 36.331 · eNB → UE",
        "structure": "RRCConnectionReject: waitTime (seconds before retry), 2 bytes on the air in this stack.",
        "flow": "Terminal event for this attach attempt; UE retries Msg1→Msg3 after the wait timer.",
    },
    "rrc_conn_reconfig": {
        "summary": "eNB reconfigures bearers/measurements after AS security is up.",
        "purpose": "Used during attach to push any additional radio bearer or measurement configuration once Security Mode Command/Complete has completed; also used later for handover and DRB add/modify.",
        "protocol": "LTE RRC · SRB1 · DL · 3GPP TS 36.331 · eNB → UE",
        "structure": "RRCConnectionReconfiguration: radioResourceConfigDedicated, measConfig (optional), mobilityControlInfo (handover only).",
        "flow": "Follows AS Security Mode Complete in the attach sequence; UE answers with RRC Conn Reconfig Complete.",
    },
    "rrc_conn_reconfig_complete": {
        "summary": "UE confirms the reconfiguration was applied.",
        "purpose": "Acknowledges RRC Connection Reconfiguration; any new bearer/measurement config is now active.",
        "protocol": "LTE RRC · SRB1 · UL · 3GPP TS 36.331 · UE → eNB",
        "structure": "RRCConnectionReconfigurationComplete: rrc-TransactionIdentifier only.",
        "flow": "Last RRC step before/around NAS Attach Complete; attach is effectively done after this.",
    },
    "rrc_conn_release": {
        "summary": "eNB tears down the RRC connection; UE returns to IDLE.",
        "purpose": "Releases SRBs/DRBs at the end of a session (or on S1 UE Context Release Command from the MME).",
        "protocol": "LTE RRC · SRB1 · DL · 3GPP TS 36.331 · eNB → UE",
        "structure": "RRCConnectionRelease: releaseCause, optional redirectedCarrierInfo, idleModeMobilityControlInfo.",
        "flow": "Final air-interface message of a detach/idle transition; UE may reattach (new Msg1) later.",
    },
    "rrc_reest_request": {
        "summary": "UE asks to resume an RRC connection after radio link failure.",
        "purpose": "Recovers from a radio-link failure or handover failure without a full re-attach, reusing the prior security context if the eNB still has it.",
        "protocol": "LTE RRC · CCCH / SRB0 · UL · 3GPP TS 36.331 · UE → eNB",
        "structure": "RRCConnectionReestablishmentRequest: ue-Identity (C-RNTI, physCellId, shortMAC-I), reestablishmentCause.",
        "flow": "Alternative to a fresh Msg1→Msg3 attach; succeeds only if the eNB has retained UE context.",
    },
    "rrc_reest_complete": {
        "summary": "UE confirms reestablishment; SRB1/security restored.",
        "purpose": "Completes the reestablishment procedure; prior radio bearers resume without a new NAS Attach.",
        "protocol": "LTE RRC · SRB1 · UL · 3GPP TS 36.331 · UE → eNB",
        "structure": "RRCConnectionReestablishmentComplete: rrc-TransactionIdentifier.",
        "flow": "Ends the reestablishment path; UE returns to normal RRC_CONNECTED.",
    },
    "as_security_mode_command": {
        "summary": "eNB activates AS (RRC/UP) ciphering and integrity.",
        "purpose": "Selects EEA/EIA algorithms and derives KRRCenc/KRRCint/KUPenc from the KeNB so SRB1 (and later DRBs) are integrity-protected and ciphered.",
        "protocol": "LTE RRC · SRB1 · DL · 3GPP TS 36.331",
        "structure": "SecurityModeCommand: securityConfigSMC (cipheringAlgorithm, integrityProtAlgorithm).",
        "flow": "Runs in parallel with NAS security during attach; UE responds with Security Mode Complete, then UE Capability Enquiry follows.",
    },
    "as_security_mode_complete": {
        "summary": "UE confirms AS security is active on SRB1.",
        "purpose": "Acknowledges the Security Mode Command; from this point SRB1 traffic is integrity-protected and ciphered.",
        "protocol": "LTE RRC · SRB1 · UL · 3GPP TS 36.331",
        "structure": "SecurityModeComplete: empty body (rrc-TransactionIdentifier only).",
        "flow": "Precedes UE Capability Enquiry and the RRC Connection Reconfiguration that finishes attach.",
    },
    "ue_cap_enquiry": {
        "summary": "eNB asks the UE for its radio capabilities.",
        "purpose": "Collects UE-EUTRA-Capability (bands, CA combos, MIMO layers, feature groups) for admission control and later RRC reconfiguration.",
        "protocol": "LTE RRC · SRB1 · DL · 3GPP TS 36.331",
        "structure": "UECapabilityEnquiry: ue-CapabilityRequest (list of RAT types, usually eutra).",
        "flow": "Runs after AS security is up, before the eNB finalizes Initial Context Setup with the MME.",
    },
    "ue_cap_information": {
        "summary": "UE reports supported bands, CA combinations, and feature sets.",
        "purpose": "Returns UE-EUTRA-Capability so the eNB/MME know what the UE can actually do (bands, carrier aggregation, MIMO).",
        "protocol": "LTE RRC · SRB1 · UL · 3GPP TS 36.331",
        "structure": "UECapabilityInformation: ue-CapabilityRAT-ContainerList (often a sizeable ASN.1 blob).",
        "flow": "Last RRC-only step before bearer setup completes attach.",
    },
    "s1ap_ue_cap_info_indication": {
        "summary": "eNB uploads the UE's radio capability to the MME.",
        "purpose": "Stores the UE-EUTRA-Capability container at the MME so it's available for future paging, handover, and capability-dependent decisions without re-querying the UE.",
        "protocol": "S1AP · TS 36.413 · eNB → MME",
        "structure": "UECapabilityInfoIndication: MME/eNB UE S1AP IDs, UE Radio Capability (raw RRC UECapabilityInformation container).",
        "flow": "Sent right after the eNB receives RRC UE Capability Information, just before Initial Context Setup Response finishes the S1 leg of attach.",
    },
    "dl_info_transfer": {
        "summary": "RRC carries a downlink NAS PDU transparently to the UE.",
        "purpose": "Generic envelope the eNB uses to relay AMF/MME-originated NAS messages (e.g. Authentication Request, Security Mode Command, Attach Accept) once SRB1 exists.",
        "protocol": "LTE RRC · SRB1 · DL · 3GPP TS 36.331",
        "structure": "DLInformationTransfer: dedicatedInfoNAS (raw NAS octets, one message per transfer).",
        "flow": "Used for every DL NAS step of attach after RRC Connection Setup.",
    },
    "ul_info_transfer": {
        "summary": "RRC carries an uplink NAS PDU transparently toward the core.",
        "purpose": "Generic envelope the UE uses to send NAS responses (Authentication Response, Security Mode Complete, etc.) once SRB1 exists.",
        "protocol": "LTE RRC · SRB1 · UL · 3GPP TS 36.331",
        "structure": "ULInformationTransfer: dedicatedInfoNAS.",
        "flow": "eNB unwraps this and forwards it to the MME as an S1AP Uplink NAS Transport.",
    },
    "nas_attach_request": {
        "summary": "EMM Attach Request: UE asks to register on the LTE network.",
        "purpose": "First NAS message of attach. Presents the UE's identity (IMSI or old GUTI), UE network capability (EEA/EIA support), and piggy-backs an ESM PDN Connectivity Request for the default bearer.",
        "protocol": "LTE NAS (EMM) · TS 24.301 · carried in RRC Connection Setup Complete",
        "structure": "Attach Request: EPS attach type, NAS key set identifier, EPS mobile identity, UE network capability, ESM message container (PDN Connectivity Request).",
        "flow": "Triggers the eNB's S1AP Initial UE Message to the MME; MME replies with Authentication Request.",
    },
    "nas_attach_accept": {
        "summary": "MME accepts the attach; assigns GUTI, TAI list, and default bearer.",
        "purpose": "Completes EMM attach: grants a GUTI, TAI list, and EPS bearer context (via the embedded Activate Default EPS Bearer Context Request) so the UE gets an IP address.",
        "protocol": "LTE NAS (EMM) · TS 24.301 · DL · carried in RRC Connection Reconfiguration or DL Information Transfer",
        "structure": "Attach Accept: EPS attach result, T3412 timer, TAI list, GUTI, ESM message container (Activate Default EPS Bearer Context Request with allocated PDN address).",
        "flow": "UE replies with NAS Attach Complete (and Activate Default EPS Bearer Context Accept) to finish attach.",
    },
    "nas_attach_complete": {
        "summary": "UE confirms attach; default EPS bearer is now active.",
        "purpose": "Final EMM step: acknowledges Attach Accept and the default EPS bearer, ending the attach procedure with a usable IP/bearer.",
        "protocol": "LTE NAS (EMM/ESM) · TS 24.301 · UL",
        "structure": "Attach Complete: ESM message container (Activate Default EPS Bearer Context Accept).",
        "flow": "Attach is done after this; MME sends a GTP-C Modify Bearer Request to the S/P-GW to finish the data path.",
    },
    "nas_auth_request": {
        "summary": "MME/HSS challenges the UE with RAND + AUTN (EPS-AKA).",
        "purpose": "Mutual authentication: the network proves it shares the subscriber key (AUTN) and the UE must compute RES from RAND using its USIM key to prove its own identity.",
        "protocol": "LTE NAS (EMM) · TS 24.301 · DL · carried in DL Information Transfer",
        "structure": "Authentication Request: NAS key set identifier, RAND (16 B), AUTN (16 B).",
        "flow": "Sits between Attach Request and Security Mode Command; a failure here aborts the attach with no IP ever assigned. No PER trace is captured for NAS-layer messages in this log format — see the written description above for the full procedure.",
    },
    "nas_auth_response": {
        "summary": "UE returns RES computed from RAND, proving its credentials.",
        "purpose": "UE derives RES (and session keys) from RAND/AUTN using its USIM key and returns RES so the MME can verify it against the HSS-computed value.",
        "protocol": "LTE NAS (EMM) · TS 24.301 · UL · carried in UL Information Transfer",
        "structure": "Authentication Response: RES (4–16 B depending on AKA variant).",
        "flow": "On success the MME immediately sends NAS Security Mode Command; on failure it sends Authentication Reject and tears down the attempt. No PER trace exists for this NAS message — see the written description above for the full procedure.",
    },
    "nas_sec_mode_command": {
        "summary": "MME selects NAS ciphering/integrity algorithms and activates NAS security.",
        "purpose": "Picks EEA/EIA algorithms, establishes the NAS security context (KNASenc/KNASint) derived from the AKA keys, and from this point every NAS message is integrity-protected (and usually ciphered).",
        "protocol": "LTE NAS (EMM) · TS 24.301 · DL · carried in DL Information Transfer",
        "structure": "Security Mode Command: selected NAS security algorithms, ngKSI-equivalent key set ID, replayed UE security capabilities, optional IMEISV request.",
        "flow": "Follows a successful Authentication Response; UE answers with NAS Security Mode Complete, unlocking encrypted Attach Accept. No over-the-air PER bytes are logged for NAS PDUs in this stack — the field above gives the fuller picture instead.",
    },
    "nas_sec_mode_complete": {
        "summary": "UE confirms NAS security algorithms; security context now active.",
        "purpose": "Acknowledges the Security Mode Command; all subsequent NAS messages (notably Attach Accept) are now integrity-protected and ciphered with the new context.",
        "protocol": "LTE NAS (EMM) · TS 24.301 · UL · carried in UL Information Transfer",
        "structure": "Security Mode Complete: optional IMEISV, optional NAS message container.",
        "flow": "Unlocks Attach Accept; the MME proceeds to build the S1AP Initial Context Setup Request next. As with other NAS-only messages here, there's no captured PER hex for this PDU — see the description above for the complete picture.",
    },
    "nas_esm_info_response": {
        "summary": "UE reports the APN it wants for the default PDN connection.",
        "purpose": "Answers an ESM Information Request (sent when the Attach Request set esmInformationTransferFlag) with the APN and/or protocol configuration options the UE wants to use.",
        "protocol": "LTE NAS (ESM) · TS 24.301 · UL",
        "structure": "ESM Information Response: procedure transaction ID, APN, protocol configuration options (optional).",
        "flow": "Lets the MME pick the right PDN/APN before sending the Create Session Request to the S/P-GW.",
    },
    "nas_emm_information": {
        "summary": "MME pushes operator name / local time info to the UE (cosmetic).",
        "purpose": "Optional EMM procedure that sends network name and/or universal time + time zone for display purposes; has no effect on the bearer or security state.",
        "protocol": "LTE NAS (EMM) · TS 24.301 · DL",
        "structure": "EMM Information: full network name, short network name, universal time and local time zone (all optional).",
        "flow": "Often sent right after Attach Complete as a courtesy message; safe to ignore when debugging attach failures.",
    },
    "s1ap_setup": {
        "summary": "eNB registers with the MME over S1AP (control-plane bring-up).",
        "purpose": "Establishes the S1 application-layer association: eNB advertises its name/ID and served PLMN/TAC list; MME confirms it can serve that tracking area.",
        "protocol": "S1AP over SCTP (S1-MME) · 3GPP TS 36.413 · eNB ↔ MME",
        "structure": "S1SetupRequest: Global eNB ID, Supported TAs (TAC + PLMN list), Default Paging DRX. S1SetupResponse: Served GUMMEIs, Relative MME Capacity.",
        "flow": "Runs once at eNB boot, before any UE can attach — if this never completes, every RRC Connection Request gets rejected with 'MME isn't connected'.",
    },
    "s1ap_initial_ue_message": {
        "summary": "eNB forwards the UE's first NAS message (Attach Request) to the MME.",
        "purpose": "Relays the NAS PDU extracted from RRC Connection Setup Complete, allocates an eNB-UE S1AP ID, and reports TAI/E-UTRAN CGI and the RRC establishment cause.",
        "protocol": "S1AP · TS 36.413 · eNB → MME",
        "structure": "InitialUEMessage: eNB UE S1AP ID, NAS-PDU (Attach Request), TAI, E-UTRAN CGI, RRC Establishment Cause.",
        "flow": "First per-UE S1AP message; MME begins authentication after this.",
    },
    "s1ap_dl_nas_transport": {
        "summary": "MME sends a NAS PDU down to the eNB for the UE.",
        "purpose": "Core-to-UE NAS delivery path on S1; the eNB unwraps the NAS-PDU and forwards it over RRC DL Information Transfer.",
        "protocol": "S1AP · TS 36.413 · MME → eNB",
        "structure": "DownlinkNASTransport: MME UE S1AP ID, eNB UE S1AP ID, NAS-PDU.",
        "flow": "Used for Authentication Request and NAS Security Mode Command during attach.",
    },
    "s1ap_ul_nas_transport": {
        "summary": "eNB forwards a UE NAS PDU up to the MME.",
        "purpose": "UE-to-core NAS relay; pairs with RRC UL Information Transfer on the air interface.",
        "protocol": "S1AP · TS 36.413 · eNB → MME",
        "structure": "UplinkNASTransport: MME/eNB UE S1AP IDs, NAS-PDU, EUTRAN CGI, TAI.",
        "flow": "Used for Authentication Response and NAS Security Mode Complete during attach.",
    },
    "s1ap_initial_ctx_setup_req": {
        "summary": "MME asks the eNB to create the UE context: KeNB, QoS, default bearer.",
        "purpose": "Provides the derived KeNB, UE security capabilities, and the E-RAB (bearer) to set up, plus the Attach Accept NAS PDU to deliver to the UE.",
        "protocol": "S1AP · TS 36.413 · MME → eNB",
        "structure": "InitialContextSetupRequest: UE Security Capabilities, Security Key (KeNB), E-RAB to be Setup List (QCI, S1-U TEID/address), NAS-PDU (Attach Accept).",
        "flow": "After this the eNB runs AS Security Mode Command, UE Capability Enquiry, and RRC Connection Reconfiguration to actually build the radio bearer.",
    },
    "s1ap_initial_ctx_setup_resp": {
        "summary": "eNB confirms UE context and E-RAB (bearer) setup.",
        "purpose": "Reports the E-RABs successfully set up, including the eNB's GTP-U TEID/address for the S1-U user-plane tunnel.",
        "protocol": "S1AP · TS 36.413 · eNB → MME",
        "structure": "InitialContextSetupResponse: E-RAB Setup List (E-RAB ID, eNB GTP-U TEID/address).",
        "flow": "Lets the MME send a GTP-C Modify Bearer Request to the S/P-GW, completing the user-plane path.",
    },
    "s1ap_ctx_release_request": {
        "summary": "eNB asks the MME to release the UE's S1/NGAP context.",
        "purpose": "Signals a radio-link problem or local release decision and requests the core tear down the UE's bearers and S1 context.",
        "protocol": "S1AP · TS 36.413 · eNB → MME",
        "structure": "UEContextReleaseRequest: MME/eNB UE S1AP IDs, Cause.",
        "flow": "Starts the detach/release sequence; MME answers with UE Context Release Command.",
    },
    "s1ap_ctx_release_command": {
        "summary": "MME orders the eNB to release the UE's radio and S1 context.",
        "purpose": "Instructs the eNB to tear down E-RABs and S1AP IDs for the UE, normally after a GTP-C Release Access Bearers exchange with the S/P-GW.",
        "protocol": "S1AP · TS 36.413 · MME → eNB",
        "structure": "UEContextReleaseCommand: MME/eNB UE S1AP IDs, Cause.",
        "flow": "eNB sends RRC Connection Release to the UE, then confirms with UE Context Release Complete.",
    },
    "s1ap_ctx_release_complete": {
        "summary": "eNB confirms the UE context has been released.",
        "purpose": "Final handshake of the release procedure; the MME can now consider the UE fully detached from this eNB.",
        "protocol": "S1AP · TS 36.413 · eNB → MME",
        "structure": "UEContextReleaseComplete: MME/eNB UE S1AP IDs.",
        "flow": "Ends the detach/release sequence started by UE Context Release Request or Command.",
    },
}

# Map dashboard 4G labels (exact or prefix) to catalog keys.
# Order matters: more specific prefixes must come before shorter ones they contain
# (e.g. "NAS Security Mode Command" before bare "Security Mode Command").
LABEL_RULES_4G: list[tuple[str, str]] = [
    ("Cell found", "cell_found"),
    ("RACH transmission", "rach_tx"),
    ("SIB1", "sib1"),
    ("systemInformation", "sib2"),
    ("PRACH preamble", "prach_msg1"),
    ("Random Access Response", "rar_msg2"),
    ("RRC Connection Request", "rrc_conn_request"),
    ("RRC Connection Setup (Msg4)", "rrc_conn_setup"),
    ("RRC Connection Setup Complete", "rrc_conn_setup_complete"),
    ("RRC Connection Reject", "rrc_conn_reject"),
    ("RRC Connection Reconfiguration", "rrc_conn_reconfig"),
    ("RRC Conn Reconfig Complete", "rrc_conn_reconfig_complete"),
    ("RRC Connection Release", "rrc_conn_release"),
    ("RRC Connection Reestablishment Request", "rrc_reest_request"),
    ("RRC Connection Reestablishment Complete", "rrc_reest_complete"),
    ("NAS Attach Request", "nas_attach_request"),
    ("NAS Attach Accept", "nas_attach_accept"),
    ("NAS Attach Complete", "nas_attach_complete"),
    ("NAS Authentication Request", "nas_auth_request"),
    ("NAS Authentication Response", "nas_auth_response"),
    ("NAS Security Mode Command", "nas_sec_mode_command"),
    ("NAS Security Mode Complete", "nas_sec_mode_complete"),
    ("NAS ESM information response", "nas_esm_info_response"),
    ("NAS EMM Information", "nas_emm_information"),
    ("Security Mode Command", "as_security_mode_command"),
    ("Security Mode Complete", "as_security_mode_complete"),
    ("UE Capability Enquiry", "ue_cap_enquiry"),
    ("UE Capability Information", "ue_cap_information"),
    ("DL Information Transfer", "dl_info_transfer"),
    ("UL Information Transfer", "ul_info_transfer"),
    ("S1AP UE Capability Info Indication", "s1ap_ue_cap_info_indication"),
    ("UECapabilityInfoIndication", "s1ap_ue_cap_info_indication"),
    ("S1AP Setup", "s1ap_setup"),
    ("s1SetupRequest", "s1ap_setup"),
    ("S1SetupResponse", "s1ap_setup"),
    ("S1AP Initial UE Message", "s1ap_initial_ue_message"),
    ("S1AP DL NAS Transport", "s1ap_dl_nas_transport"),
    ("S1AP UL NAS Transport", "s1ap_ul_nas_transport"),
    ("S1AP Initial Context Setup Request", "s1ap_initial_ctx_setup_req"),
    ("InitialContextSetupRequest", "s1ap_initial_ctx_setup_req"),
    ("S1AP Initial Context Setup Response", "s1ap_initial_ctx_setup_resp"),
    ("InitialContextSetupResponse", "s1ap_initial_ctx_setup_resp"),
    ("S1AP UE Context Release Request", "s1ap_ctx_release_request"),
    ("UEContextReleaseRequest", "s1ap_ctx_release_request"),
    ("S1AP UE Context Release Command", "s1ap_ctx_release_command"),
    ("UEContextReleaseCommand", "s1ap_ctx_release_command"),
    ("S1AP UE Context Release Complete", "s1ap_ctx_release_complete"),
    ("UEContextReleaseComplete", "s1ap_ctx_release_complete"),
]

_UNKNOWN_4G = {
    "key": "unknown",
    "summary": "4G signaling event parsed from srsUE/srseNB logs.",
    "purpose": "See the log excerpt below for the raw srsRAN_4G log line (ASN.1/hex detail where available).",
    "protocol": "See the log layer and route shown in the header.",
    "structure": "Refer to the decoded ASN.1 in the log excerpt (3GPP TS 36.331 / 36.413 / 24.301).",
    "flow": "Part of the 4G LTE attach procedure shown in the ladder.",
}


def lookup_message_info_4g(label: str) -> dict:
    for prefix, key in LABEL_RULES_4G:
        if label.startswith(prefix) or prefix in label:
            info = MESSAGE_INFO_4G.get(key)
            if info:
                return {"key": key, **info}
    return dict(_UNKNOWN_4G)
