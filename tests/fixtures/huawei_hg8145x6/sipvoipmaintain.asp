<script>
function stLineURI(Domain, URI){ this.Domain=Domain; this.URI=URI; }
function stLine(Domain, DirectoryNumber, PhyReferenceList, Status, CallState,RegisterError){ this.Domain=Domain; this.DirectoryNumber=DirectoryNumber; this.PhyReferenceList=PhyReferenceList; this.Status=Status; this.CallState=CallState; this.RegisterError=RegisterError; }
var AllLineURI = new Array(new stLineURI("InternetGatewayDevice.Services.VoiceService.1.VoiceProfile.1.Line.1.SIP","[REDACTED]"),null);
var AllLine = new Array(new stLine("InternetGatewayDevice.Services.VoiceService.1.VoiceProfile.1.Line.1","","1","Disabled","Idle","ERROR_WAN_NOT_CONFIGURED"),null);
</script>