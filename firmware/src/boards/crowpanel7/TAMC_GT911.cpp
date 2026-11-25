#include "Arduino.h"
#include "TAMC_GT911.h"
#include <Wire.h>

TAMC_GT911::TAMC_GT911(uint8_t _sda, uint8_t _scl, uint8_t _int, uint8_t _rst, uint16_t _width, uint16_t _height) :
  pinSda(_sda), pinScl(_scl), pinInt(_int), pinRst(_rst), width(_width), height(_height) {
}

void TAMC_GT911::begin(uint8_t _addr) {
  addr = _addr;
  reset();
  Serial.printf("TAMC_GT911::begin --> address 0x%02X\n", addr);
}

void TAMC_GT911::reset() {
  readBlockData(configBuf, GT911_CONFIG_START, GT911_CONFIG_SIZE);
  // Do NOT call setResolution, it corrupts the checksum because reflashConfig doesn't write the full buffer.
  // The GT911 is already factory flashed with 800x480 resolution.
}

void TAMC_GT911::calculateChecksum() {
  uint8_t checksum = 0;
  for (uint8_t i = 0; i < GT911_CONFIG_SIZE; i++) {
    checksum += configBuf[i];
  }
  checksum = (~checksum) + 1;
  configBuf[GT911_CONFIG_CHKSUM - GT911_CONFIG_START] = checksum;
}

void TAMC_GT911::reflashConfig() {
  calculateChecksum();
  writeByteData(GT911_CONFIG_CHKSUM, configBuf[GT911_CONFIG_CHKSUM - GT911_CONFIG_START]);
  writeByteData(GT911_CONFIG_FRESH, 1);
}

void TAMC_GT911::setRotation(uint8_t rot) {
  rotation = rot;
}

void TAMC_GT911::setResolution(uint16_t _width, uint16_t _height) {
  configBuf[GT911_X_OUTPUT_MAX_LOW - GT911_CONFIG_START] = lowByte(_width);
  configBuf[GT911_X_OUTPUT_MAX_HIGH - GT911_CONFIG_START] = highByte(_width);
  configBuf[GT911_Y_OUTPUT_MAX_LOW - GT911_CONFIG_START] = lowByte(_height);
  configBuf[GT911_Y_OUTPUT_MAX_HIGH - GT911_CONFIG_START] = highByte(_height);
  reflashConfig();
}

void TAMC_GT911::read(void) {
  uint8_t raw[8] = {0};
  readBlockData(raw, GT911_POINT_INFO, 8);
  uint8_t pointInfo = raw[0];
  uint8_t bufferStatus = (pointInfo >> 7) & 1;

  if (bufferStatus == 1) {
    touches = pointInfo & 0x0F;
    isTouched = (touches > 0);

    if (isTouched) {
      points[0] = readPoint(&raw[1]);
      for (uint8_t i = 1; i < touches && i < 5; i++) {
        uint8_t data[7] = {0};
        readBlockData(data, GT911_POINT_1 + i * 8, 7);
        points[i] = readPoint(data);
      }
    }
    writeByteData(GT911_POINT_INFO, 0);
  }
}

void TAMC_GT911::sleep(void) {
  writeByteData(GT911_COMMAND, 0x05);
}

TP_Point TAMC_GT911::readPoint(uint8_t *data) {
  uint16_t temp;
  uint8_t id = data[0];
  uint16_t x = data[1] + (data[2] << 8);
  uint16_t y = data[3] + (data[4] << 8);
  uint16_t size = data[5] + (data[6] << 8);
  switch (rotation) {
    case ROTATION_NORMAL:
      x = width - x;
      y = height - y;
      break;
    case ROTATION_LEFT:
      temp = x;
      x = width - y;
      y = temp;
      break;
    case ROTATION_INVERTED:
      x = x;
      y = y;
      break;
    case ROTATION_RIGHT:
      temp = x;
      x = y;
      y = height - temp;
      break;
    default:
      break;
  }
  return TP_Point(id, x, y, size);
}

void TAMC_GT911::writeByteData(uint16_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(highByte(reg));
  Wire.write(lowByte(reg));
  Wire.write(val);
  Wire.endTransmission(true);
}

uint8_t TAMC_GT911::readByteData(uint16_t reg) {
  uint8_t x = 0;
  Wire.beginTransmission(addr);
  Wire.write(highByte(reg));
  Wire.write(lowByte(reg));
  Wire.endTransmission();
  if (Wire.requestFrom((int)addr, 1) == 1) {
    x = Wire.read();
  }
  return x;
}

void TAMC_GT911::writeBlockData(uint16_t reg, uint8_t *val, uint8_t size) {
  Wire.beginTransmission(addr);
  Wire.write(highByte(reg));
  Wire.write(lowByte(reg));
  for (uint8_t i = 0; i < size; i++) {
    Wire.write(val[i]);
  }
  Wire.endTransmission(true);
}

void TAMC_GT911::readBlockData(uint8_t *buf, uint16_t reg, uint8_t size) {
  Wire.beginTransmission(addr);
  Wire.write(highByte(reg));
  Wire.write(lowByte(reg));
  Wire.endTransmission();
  if (Wire.requestFrom((int)addr, (int)size) == size) {
    for (uint8_t i = 0; i < size; i++) {
      buf[i] = Wire.read();
    }
  }
}

TP_Point::TP_Point(void) {
  id = x = y = size = 0;
}

TP_Point::TP_Point(uint8_t _id, uint16_t _x, uint16_t _y, uint16_t _size) {
  id = _id;
  x = _x;
  y = _y;
  size = _size;
}

bool TP_Point::operator==(TP_Point point) {
  return ((point.x == x) && (point.y == y) && (point.size == size));
}

bool TP_Point::operator!=(TP_Point point) {
  return ((point.x != x) || (point.y != y) || (point.size != size));
}
