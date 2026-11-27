#include "tusb.h"
#include <string.h>
static tusb_desc_device_t const dev={.bLength=sizeof(tusb_desc_device_t),.bDescriptorType=TUSB_DESC_DEVICE,.bcdUSB=0x0200,.bDeviceClass=TUSB_CLASS_MISC,.bDeviceSubClass=MISC_SUBCLASS_COMMON,.bDeviceProtocol=MISC_PROTOCOL_IAD,.bMaxPacketSize0=64,.idVendor=0xCafe,.idProduct=0x4011,.bcdDevice=0x0110,.iManufacturer=1,.iProduct=2,.iSerialNumber=3,.bNumConfigurations=1};
uint8_t const *tud_descriptor_device_cb(void){return (uint8_t const*)&dev;}
static uint8_t const cfg[]={TUD_CONFIG_DESCRIPTOR(1,2,0,TUD_CONFIG_DESC_LEN+TUD_CDC_DESC_LEN,0,100),TUD_CDC_DESCRIPTOR(0,0,0x81,8,0x02,0x82,64)};
uint8_t const *tud_descriptor_configuration_cb(uint8_t index){(void)index;return cfg;}
uint16_t const *tud_descriptor_string_cb(uint8_t index,uint16_t langid){(void)langid;static uint16_t b[32];const char *s[]={"","SIR88 lab","RP2040 SIR88","SIR88-01"};if(!index){b[1]=0x0409;b[0]=0x0304;return b;}if(index>3)return NULL;size_t n=strlen(s[index]);for(size_t i=0;i<n;i++)b[i+1]=(uint8_t)s[index][i];b[0]=0x0300|((n+1)*2);return b;}
