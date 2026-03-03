ARG BUILD_FROM
FROM $BUILD_FROM

# System dependencies
RUN apk add --no-cache \
    sqlite \
    tzdata \
    bash

# Install Python dependencies
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ /app/

# Make run.sh executable
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Build labels required by HA
ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_VERSION
LABEL \
    io.hass.name="FVE Solar Forecast" \
    io.hass.description="Solar PV production forecasting with self-learning calibration" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}"

CMD ["/run.sh"]
