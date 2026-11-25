<script>
function USERDevice(Domain,IpAddr,MacAddr,Port,IpType,DevType,DevStatus,PortType,Time,HostName,IPv4Enabled,IPv6Enabled,DeviceType,UserDevAlias,UserSpecifiedDeviceType,LeaseTimeRemaining,RealMacAddr)
{
  this.Domain=Domain; this.IpAddr=IpAddr; this.MacAddr=MacAddr; this.Port=Port; this.IpType=IpType; this.DevType=DevType; this.DevStatus=DevStatus; this.PortType=PortType; this.Time=Time; this.HostName=HostName; this.IPv4Enabled=IPv4Enabled; this.IPv6Enabled=IPv6Enabled; this.DeviceType=DeviceType; this.UserDevAlias=UserDevAlias; this.UserSpecifiedDeviceType=UserSpecifiedDeviceType; this.LeaseTimeRemaining=LeaseTimeRemaining; this.RealMacAddr=RealMacAddr;
}
var LANHostInfo = new Array(new USERDevice("InternetGatewayDevice.LANDevice.1.X_HW_UserDev.1","192.0.2.10","AA:BB:CC:00:00:01","LAN1","DHCP","--","Online","ETH","0:10","host-a","1","0","0","","0","0","AA:BB:CC:00:00:01"),new USERDevice("InternetGatewayDevice.LANDevice.1.X_HW_UserDev.2","192.0.2.11","AA:BB:CC:00:00:02","SSID1","DHCP","--","Offline","WIFI","1:20","host-b","1","0","0","","0","0","AA:BB:CC:00:00:02"),null);
</script>